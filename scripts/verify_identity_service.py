#!/usr/bin/env python3
"""Prove the Identity Service manages users safely, on the real cluster, against real Keycloak.

Run `make identity-seed`, `make migrate`, `make services` and `make authorization-seed` first.
Each call is signed as one of the real demo users, as Web BFF will sign it. Every decision comes
from the real Authorization Service. The users this creates are removed at the end.
"""

import base64
import json
import secrets
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
from gateway_routes import port_forward
from identity_seed import (
    ADMIN_SECRET_FILE,
    REALM,
    admin_client,
    demo_admin_email,
    demo_email,
    keycloak_admin,
    keycloak_public,
    read_demo_passwords,
)
from provision_postgres import read_passwords
from tls_client import ClientBundle, blocking_channel, client_bundle
from verify_identity import Realm
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.identity.v1 import identity_pb2 as pb
from windops.identity.v1 import identity_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_grpc import VerifiedIdentity, sign_identity

CONTEXT = "windops"
SYSTEM, APPLICATION = "windops-system", "windops-application"
DEPLOYMENT = "deployment/windops-identity"
AUTHORIZATION = "deployment/windops-authorization"
HOST = "windops-identity.windops-system.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
TENANT, OTHER = "north-sea", "atlas-wind"
RUN = secrets.token_hex(3)
NEW_EMAIL = f"identity-check-{RUN}@north-sea.example"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, namespace: str = SYSTEM) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={namespace}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def signing_key() -> str:
    encoded = kubectl(
        "get", "secret", "windops-internal-identity", "--output=jsonpath={.data.signing-key}"
    ).stdout
    return base64.b64decode(encoded).decode()


class Caller:
    """Calls as one real user, the way Web BFF will after checking their Keycloak token."""

    def __init__(self, channel: grpc.Channel, key: str) -> None:
        self.stub: Any = identity_pb2_grpc.IdentityServiceStub(channel)
        self.key = key

    def metadata(self, user: str, level: str | None = LEVEL_MFA) -> Any:
        identity = VerifiedIdentity(
            subject=user,
            tenant_id=TENANT,
            user_id=user,
            auth_level=level,
            auth_time=int(time.time()) if level else None,
        )
        return (("x-windops-identity", sign_identity(identity, self.key, ttl_seconds=60)),)

    def call(self, method: str, request: Any, user: str, **options: Any) -> Any:
        return getattr(self.stub, method)(
            request, metadata=self.metadata(user, **options), timeout=15
        )

    def code(self, method: str, request: Any, user: str, **options: Any) -> grpc.StatusCode:
        try:
            self.call(method, request, user, **options)
        except grpc.RpcError as error:
            status: grpc.StatusCode = error.code()
            return status
        return grpc.StatusCode.OK


def user_id(api: httpx.Client, email: str) -> str:
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    return str(found.json()[0]["id"])


def keycloak_user(api: httpx.Client, member: str) -> dict[str, Any]:
    response = api.get(f"/{REALM}/users/{member}")
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


def organization_members(api: httpx.Client, alias: str) -> set[str]:
    organizations = api.get(f"/{REALM}/organizations", params={"first": 0, "max": 500}).json()
    organization = next(item["id"] for item in organizations if item["alias"] == alias)
    members = api.get(f"/{REALM}/organizations/{organization}/members").json()
    return {str(item["id"]) for item in members}


def check_permissions(caller: Caller, admin: str, operator: str) -> None:
    check(
        caller.code("ListUsers", pb.ListUsersRequest(), operator)
        is grpc.StatusCode.PERMISSION_DENIED,
        "A Control Room Operator cannot manage users, as the Authorization Service decides",
    )
    listed = caller.call("ListUsers", pb.ListUsersRequest(page_size=200), admin)
    emails = {user.email for user in listed.users}
    check(
        {demo_admin_email(TENANT), demo_email(TENANT)} <= emails
        and demo_email(OTHER) not in emails,
        f"The Tenant Administrator sees {TENANT}'s users, and none of {OTHER}'s",
    )


def check_creation(caller: Caller, api: httpx.Client, admin: str) -> str:
    request = pb.CreateUserRequest(
        email=NEW_EMAIL, first_name="Identity", last_name="Check", idempotency_key=f"key-{RUN}"
    )
    check(
        caller.code("CreateUser", request, admin, level=LEVEL_PASSWORD)
        is grpc.StatusCode.PERMISSION_DENIED,
        "Creating a user without a recent second factor is refused (SEC-003)",
    )
    created = caller.call("CreateUser", request, admin)
    member = str(created.user.member_id)
    stored = keycloak_user(api, member)
    check(
        bool(created.temporary_password)
        and member in organization_members(api, TENANT)
        and stored["requiredActions"] == ["UPDATE_PASSWORD"],
        "A new user is in the tenant, with a temporary password they must change",
    )
    again = caller.call("CreateUser", request, admin)
    check(
        again.user.member_id == member and again.already_created and not again.temporary_password,
        "A retry returns the same user, and never the password again (GRP-007)",
    )
    return member


def check_isolation(caller: Caller, api: httpx.Client, admin: str) -> None:
    stranger = user_id(api, demo_email(OTHER))
    for method, request in (
        ("GetUser", pb.GetUserRequest(member_id=stranger)),
        ("DisableUser", pb.DisableUserRequest(member_id=stranger)),
        ("RevokeSessions", pb.RevokeSessionsRequest(member_id=stranger)),
    ):
        check(
            caller.code(method, request, admin) is grpc.StatusCode.NOT_FOUND,
            f"{method} on another tenant's user answers as if they did not exist (SEC-007)",
        )
    check(keycloak_user(api, stranger)["enabled"] is True, f"{OTHER}'s user was not touched")


def check_sessions(caller: Caller, realm: Realm, admin: str, operator: str) -> None:
    password = read_demo_passwords()[TENANT]
    tokens = realm.login(demo_email(TENANT), password)
    if tokens is None:
        raise RuntimeError("The demo operator could not sign in")
    listed = caller.call("ListSessions", pb.ListSessionsRequest(member_id=operator), admin)
    check(len(listed.sessions) >= 1, "The operator's real Keycloak session is listed")
    revoked = caller.call("RevokeSessions", pb.RevokeSessionsRequest(member_id=operator), admin)
    check(revoked.sessions_ended >= 1, "The Tenant Administrator ends the operator's sessions")
    check(
        realm.refresh(tokens.refresh).status_code == 400,
        "The operator's refresh token stops working at once (SEC-004)",
    )


def check_disable(caller: Caller, api: httpx.Client, admin: str, member: str) -> None:
    caller.call("DisableUser", pb.DisableUserRequest(member_id=member), admin)
    check(
        keycloak_user(api, member)["enabled"] is False, "Disabling a user disables them in Keycloak"
    )
    again = caller.call("DisableUser", pb.DisableUserRequest(member_id=member), admin)
    check(not again.user.enabled, "Disabling twice is harmless")
    caller.call("ReactivateUser", pb.ReactivateUserRequest(member_id=member), admin)
    check(keycloak_user(api, member)["enabled"] is True, "Reactivating them enables them again")
    check(
        caller.code("DisableUser", pb.DisableUserRequest(member_id=admin), admin)
        is grpc.StatusCode.INVALID_ARGUMENT,
        "An administrator cannot disable their own account",
    )


def check_mfa_status(caller: Caller, api: httpx.Client, admin: str) -> None:
    status = caller.call("GetMfaStatus", pb.GetMfaStatusRequest(member_id=admin), admin)
    kinds = {item["type"] for item in api.get(f"/{REALM}/users/{admin}/credentials").json()}
    check(
        status.otp_configured == ("otp" in kinds)
        and status.recovery_codes_configured == ("recovery-authn-codes" in kinds),
        f"MFA status matches what Keycloak holds ({sorted(kinds)})",
    )


def check_authorization_outage(caller: Caller, admin: str) -> None:
    """SRS 7.7: with Authorization down, user management fails closed."""
    kubectl("scale", AUTHORIZATION, "--replicas=0")
    try:
        kubectl("wait", "pod", "--selector=app.kubernetes.io/name=windops-authorization",
                "--for=delete", "--timeout=120s")  # fmt: skip
        check(
            caller.code("ListUsers", pb.ListUsersRequest(), admin) is grpc.StatusCode.UNAVAILABLE,
            "With the Authorization Service down, user management is refused, not allowed",
        )
    finally:
        kubectl("scale", AUTHORIZATION, "--replicas=1")
        kubectl("rollout", "status", AUTHORIZATION, "--timeout=180s")
    for _ in range(30):
        if caller.code("ListUsers", pb.ListUsersRequest(), admin) is grpc.StatusCode.OK:
            break
        time.sleep(2)
    check(
        caller.code("ListUsers", pb.ListUsersRequest(), admin) is grpc.StatusCode.OK,
        "It works again once Authorization returns",
    )


def check_audit(bundle: ClientBundle, key: str, admin: str) -> None:
    wanted = {
        "identity.user.created",
        "identity.user.disabled",
        "identity.user.reactivated",
        "identity.sessions.revoked",
    }
    identity = VerifiedIdentity(admin, TENANT, admin)
    metadata = (("x-windops-identity", sign_identity(identity, key, ttl_seconds=60)),)
    found: set[str] = set()
    for _ in range(30):
        with (
            port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
            blocking_channel(port, AUDIT_HOST, bundle) as channel,
        ):
            stub: Any = audit_pb2_grpc.AuditServiceStub(channel)
            records = stub.ListAuditRecords(
                audit_pb2.ListAuditRecordsRequest(page_size=500), metadata=metadata, timeout=10
            ).records
        found = {r.action for r in records if r.producer == "identity" and r.actor_id == admin}
        if wanted <= found:
            break
        time.sleep(2)
    check(wanted <= found, f"{TENANT}'s audit chain holds all {len(wanted)} kinds of change")


def cleanup(base: str, password: str) -> None:
    """Sign in again, because an admin token lasts 60 seconds and the checks take longer."""
    with admin_client(base, password) as api:
        found = api.get(f"/{REALM}/users", params={"email": NEW_EMAIL, "exact": "true"})
        found.raise_for_status()
        for user in found.json():
            api.delete(f"/{REALM}/users/{user['id']}").raise_for_status()


def run(public: str, base: str, password: str) -> None:
    status = kubectl("rollout", "status", DEPLOYMENT, "--timeout=180s")
    check(status.returncode == 0, "The Identity Service is running")
    bundle, key = client_bundle(), signing_key()
    realm = Realm(public)
    with admin_client(base, password) as api:
        admin, operator = user_id(api, demo_admin_email(TENANT)), user_id(api, demo_email(TENANT))
        try:
            with (
                port_forward(DEPLOYMENT, SYSTEM, 50051) as port,
                blocking_channel(port, HOST, bundle) as channel,
            ):
                caller = Caller(channel, key)
                check_permissions(caller, admin, operator)
                member = check_creation(caller, api, admin)
                check_isolation(caller, api, admin)
                check_sessions(caller, realm, admin, operator)
                check_disable(caller, api, admin, member)
                check_mfa_status(caller, api, admin)
                check_authorization_outage(caller, admin)
            check_audit(bundle, key, admin)
        finally:
            cleanup(base, password)


def main() -> int:
    password = read_passwords(ADMIN_SECRET_FILE)["password"]
    try:
        with keycloak_public(through_gateway=False) as public, keycloak_admin() as base:
            run(public, base, password)
    except (RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, json.JSONDecodeError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Identity Service check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
