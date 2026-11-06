#!/usr/bin/env python3
"""Prove the Authorization Service decides and audits correctly on the real local cluster.

Run `make migrate` and `make services` first. Each run uses its own synthetic tenant and users,
signed as Web BFF will sign them, so it needs no Keycloak login.
"""

import base64
import secrets
import subprocess
import sys
import time
from typing import Any

import grpc
from gateway_routes import port_forward
from settings import KUBE_CONTEXT
from tls_client import ClientBundle, blocking_channel, client_bundle
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.authorization.v1 import authorization_pb2 as pb
from windops.authorization.v1 import authorization_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_grpc import VerifiedIdentity, sign_identity

CONTEXT = KUBE_CONTEXT
SYSTEM, APPLICATION = "windops-system", "windops-application"
DEPLOYMENT = "deployment/windops-authorization"
HOST = "windops-authorization.windops-system.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
PLATFORM = "windops-platform"
RUN = secrets.token_hex(3)
TENANT, OTHER = f"authz-check-{RUN}", f"authz-other-{RUN}"
ADMIN, OPERATOR, PLATFORM_ADMIN = f"admin-{RUN}", f"operator-{RUN}", f"platform-{RUN}"
FARM, OTHER_FARM = "farm-1", "farm-2"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, namespace: str = SYSTEM) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={namespace}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def bootstrap(tenant: str, user: str, role: str) -> subprocess.CompletedProcess[str]:
    return kubectl(
        "exec", DEPLOYMENT, "--", "python", "-m", "windops_authorization.bootstrap",
        "--tenant", tenant, "--user", user, "--role", role,
    )  # fmt: skip


def signing_key() -> str:
    encoded = kubectl(
        "get", "secret", "windops-internal-identity", "--output=jsonpath={.data.signing-key}"
    ).stdout
    return base64.b64decode(encoded).decode()


class Caller:
    """Calls as one user, the way Web BFF will after checking their Keycloak token."""

    def __init__(self, channel: grpc.Channel, key: str) -> None:
        self.stub: Any = authorization_pb2_grpc.AuthorizationServiceStub(channel)
        self.key = key

    def metadata(self, user: str, tenant: str, level: str | None = LEVEL_MFA) -> Any:
        identity = VerifiedIdentity(
            subject=user,
            tenant_id=tenant,
            user_id=user,
            auth_level=level,
            auth_time=int(time.time()) if level else None,
        )
        return (("x-windops-identity", sign_identity(identity, self.key, ttl_seconds=60)),)

    def allowed(self, user: str, tenant: str, permission: str, **scope: str) -> pb.CheckResponse:
        request = pb.CheckRequest(
            permission=permission,
            scope=pb.Scope(**scope),
            target_tenant_id="" if tenant == self._home(user) else tenant,
        )
        response: pb.CheckResponse = self.stub.Check(
            request, metadata=self.metadata(user, self._home(user)), timeout=10
        )
        return response

    @staticmethod
    def _home(user: str) -> str:
        return PLATFORM if user == PLATFORM_ADMIN else TENANT

    def code(self, method: str, request: Any, user: str, **options: Any) -> grpc.StatusCode:
        try:
            getattr(self.stub, method)(
                request, metadata=self.metadata(user, self._home(user), **options), timeout=10
            )
        except grpc.RpcError as error:
            status: grpc.StatusCode = error.code()
            return status
        return grpc.StatusCode.OK


def check_bootstrap() -> None:
    check(
        bootstrap(TENANT, ADMIN, "tenant-administrator").stdout.strip() == "Granted",
        "The bootstrap gives a new tenant its first Tenant Administrator",
    )
    check(
        bootstrap(TENANT, ADMIN, "tenant-administrator").stdout.strip() == "Already granted",
        "Running it again changes nothing",
    )
    refused = bootstrap(TENANT, PLATFORM_ADMIN, "platform-administrator")
    check(
        refused.returncode != 0,
        "Even the bootstrap cannot make a Platform Administrator in a customer tenant",
    )
    check(
        bootstrap(PLATFORM, PLATFORM_ADMIN, "platform-administrator").returncode == 0,
        "The platform tenant gets its Platform Administrator",
    )


def check_decisions(caller: Caller) -> None:
    check(
        not caller.allowed(OPERATOR, TENANT, "turbine.read").allowed,
        "A user with no role is denied everything",
    )
    grant = pb.GrantRoleRequest(
        grantee_id=OPERATOR, role="control-room-operator", scope=pb.Scope(wind_farm_id=FARM)
    )
    check(
        caller.code("GrantRole", grant, ADMIN, level=LEVEL_PASSWORD)
        is grpc.StatusCode.PERMISSION_DENIED,
        "Granting a role without a recent second factor is refused (SEC-003)",
    )
    check(
        caller.code("GrantRole", grant, OPERATOR) is grpc.StatusCode.PERMISSION_DENIED,
        "A user without roles.manage cannot grant roles",
    )
    granted: pb.GrantRoleResponse = caller.stub.GrantRole(
        grant, metadata=caller.metadata(ADMIN, TENANT), timeout=10
    )
    check(granted.grant.granted_by == ADMIN, "The Tenant Administrator grants a role to one farm")
    check(
        caller.allowed(OPERATOR, TENANT, "alarm.acknowledge", wind_farm_id=FARM).allowed,
        "The operator may acknowledge alarms on that farm",
    )
    check(
        not caller.allowed(OPERATOR, TENANT, "alarm.acknowledge", wind_farm_id=OTHER_FARM).allowed,
        "but not on another farm (RBAC-002)",
    )
    check(
        not caller.allowed(OPERATOR, TENANT, "maintenance.approve", wind_farm_id=FARM).allowed,
        "and cannot use a permission the role does not have",
    )
    elsewhere = pb.GrantRoleRequest(
        target_tenant_id=OTHER, grantee_id=ADMIN, role="tenant-administrator"
    )
    check(
        caller.code("GrantRole", elsewhere, ADMIN) is grpc.StatusCode.PERMISSION_DENIED,
        "A Tenant Administrator cannot act in another tenant by naming it (RBAC-001)",
    )
    caller.stub.RevokeRole(
        pb.RevokeRoleRequest(grant_id=granted.grant.grant_id),
        metadata=caller.metadata(ADMIN, TENANT),
        timeout=10,
    )
    check(
        not caller.allowed(OPERATOR, TENANT, "alarm.acknowledge", wind_farm_id=FARM).allowed,
        "Revoking the role ends the access at once",
    )


def check_break_glass(caller: Caller) -> None:
    check(
        not caller.allowed(PLATFORM_ADMIN, TENANT, "telemetry.read").allowed,
        "A Platform Administrator cannot read a tenant's data by default (RBAC-003)",
    )
    request = pb.StartBreakGlassRequest(
        target_tenant_id=TENANT,
        reason=f"Authorization check {RUN}, testing break-glass access",
        duration_minutes=1,
    )
    check(
        caller.code("StartBreakGlass", request, PLATFORM_ADMIN, level=LEVEL_PASSWORD)
        is grpc.StatusCode.PERMISSION_DENIED,
        "Break-glass needs a recent second factor",
    )
    check(
        caller.code("StartBreakGlass", request, ADMIN) is grpc.StatusCode.PERMISSION_DENIED,
        "A Tenant Administrator cannot use break-glass",
    )
    caller.stub.StartBreakGlass(
        request, metadata=caller.metadata(PLATFORM_ADMIN, PLATFORM), timeout=10
    )
    reading = caller.allowed(PLATFORM_ADMIN, TENANT, "telemetry.read")
    check(reading.allowed and reading.break_glass, "Break-glass allows reading the tenant's data")
    check(
        not caller.allowed(PLATFORM_ADMIN, TENANT, "maintenance.approve").allowed,
        "but never changing it",
    )
    print("Waiting for the one-minute grant to expire", flush=True)
    time.sleep(65)
    check(
        not caller.allowed(PLATFORM_ADMIN, TENANT, "telemetry.read").allowed,
        "The grant expires by itself",
    )


def audit_actions(bundle: ClientBundle, key: str) -> set[str]:
    identity = VerifiedIdentity("authz-check", TENANT, "authz-check")
    metadata = (("x-windops-identity", sign_identity(identity, key, ttl_seconds=60)),)
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, bundle) as channel,
    ):
        stub: Any = audit_pb2_grpc.AuditServiceStub(channel)
        response = stub.ListAuditRecords(
            audit_pb2.ListAuditRecordsRequest(page_size=200), metadata=metadata, timeout=10
        )
    return {record.action for record in response.records if record.producer == "authorization"}


def check_audit(bundle: ClientBundle, key: str) -> None:
    """The records went through the outbox, JetStream and the Audit Service, into the chain."""
    wanted = {
        "authorization.denied",
        "authorization.role.granted.tenant-administrator",
        "authorization.role.granted.control-room-operator",
        "authorization.role.revoked.control-room-operator",
        "authorization.break_glass.started",
        "authorization.break_glass.used",
    }
    # Reading the trail needs audit.read, so the reader is made an Auditor first (SRS 3.2).
    reader = bootstrap(TENANT, "authz-check", "auditor")
    if reader.returncode != 0:
        raise RuntimeError(f"Could not make the reader an Auditor: {reader.stderr.strip()}")
    found: set[str] = set()
    for _ in range(30):
        found = audit_actions(bundle, key)
        if wanted <= found:
            break
        time.sleep(2)
    check(wanted <= found, f"The tenant's audit chain holds all {len(wanted)} kinds of record")


def main() -> int:
    try:
        status = kubectl("rollout", "status", DEPLOYMENT, "--timeout=180s")
        check(status.returncode == 0, "The Authorization Service is running")
        check_bootstrap()
        bundle = client_bundle()
        key = signing_key()
        with (
            port_forward(DEPLOYMENT, SYSTEM, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            caller = Caller(channel, key)
            check_decisions(caller)
            check_break_glass(caller)
        check_audit(bundle, key)
    except (RuntimeError, OSError, grpc.RpcError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Authorization check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
