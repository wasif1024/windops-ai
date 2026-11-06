#!/usr/bin/env python3
"""Prove the administrator console's API on the real cluster, through the gateway (SRS 9.4).

Run `make services`, `make gateway-routes`, `make identity-seed`, `make authorization-seed` and
`make tenant-seed` first. Needs local port 9443 free, as Keycloak's pages name that address.

It signs in as a temporary Tenant Administrator, by password only, so it can read everything but
every change asks for a second factor. The user is deleted afterwards. Their grant stays, ended by
nobody, for an ID that can no longer sign in, because grants are kept, never deleted (RBAC-005).
"""

import secrets
import subprocess
import sys
import time
import uuid

import httpx
from identity_seed import (
    ADMIN_SECRET_FILE,
    PUBLIC_URL,
    REALM,
    TENANTS,
    admin_client,
    demo_email,
    ensure_member,
    ensure_tenant,
    keycloak_admin,
    keycloak_public,
    read_demo_passwords,
    strong_password,
)
from provision_postgres import read_passwords
from settings import KUBE_CONTEXT
from verify_identity import Realm, create_user, delete_user

CONTEXT = KUBE_CONTEXT
SYSTEM = "windops-system"
API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"
TENANT = "north-sea"
ADMIN_EMAIL = f"admin-api-{secrets.token_hex(3)}@windops-check.example"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def call(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, str | int] | None = None,
    key: bool = True,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if method != "GET" and key:
        headers["Idempotency-Key"] = uuid.uuid4().hex
    return httpx.request(method, f"{API}{path}", headers=headers, params=params, timeout=20)


def make_tenant_administrator(user_id: str) -> None:
    result = subprocess.run(
        [
            "kubectl", "--context", CONTEXT, f"--namespace={SYSTEM}", "exec",
            "deployment/windops-authorization", "--", "python", "-m",
            "windops_authorization.bootstrap",
            "--tenant", TENANT, "--user", user_id, "--role", "tenant-administrator",
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"Could not grant the role: {result.stderr.strip()}")


def check_reads(admin: str, admin_id: str, operator_id: str) -> None:
    users = call("GET", "/admin/users", admin, params={"page_size": 200})
    emails = {item["email"] for item in users.json().get("users", [])}
    check(
        users.status_code == 200 and {ADMIN_EMAIL, demo_email(TENANT)} <= emails,
        "A Tenant Administrator lists the tenant's users",
    )
    other = {demo_email(alias) for alias in TENANTS if alias != TENANT}
    check(not emails & other, "and sees no other tenant's users")
    mfa = call("GET", f"/admin/users/{operator_id}/mfa", admin)
    check(
        mfa.status_code == 200 and set(mfa.json()) >= {"otp_configured", "setup_required"},
        "sees a user's MFA status",
    )
    sessions = call("GET", f"/admin/users/{admin_id}/sessions", admin)
    check(
        sessions.status_code == 200 and len(sessions.json()["sessions"]) >= 1,
        "sees a user's sessions, including their own",
    )
    grants = call("GET", "/admin/role-grants", admin, params={"user_id": admin_id})
    roles = {item["role"] for item in grants.json().get("grants", [])}
    check(grants.status_code == 200 and "tenant-administrator" in roles, "lists role grants")
    mapping = call("GET", "/admin/roles/auditor/permissions", admin)
    check(
        mapping.status_code == 200 and "audit.read" in mapping.json()["permissions"],
        "and reads what a role gives (SRS 3.2)",
    )


def check_step_up(admin: str, operator_id: str) -> None:
    changed = call("POST", f"/admin/users/{operator_id}/disable", admin)
    check(
        changed.status_code == 401
        and changed.json().get("code") == "step_up_required"
        and 'acr_values="mfa"' in changed.headers.get("WWW-Authenticate", ""),
        "A change without a recent second factor asks for one (SEC-003)",
    )
    still = call("GET", f"/admin/users/{operator_id}", admin)
    check(still.status_code == 200 and still.json()["enabled"], "and changes nothing")
    unkeyed = call("POST", f"/admin/users/{operator_id}/disable", admin, key=False)
    check(
        unkeyed.status_code == 422
        and unkeyed.headers.get("content-type") == "application/problem+json",
        "A change with no Idempotency-Key is refused before any service (GRP-007)",
    )


def check_branding(admin: str) -> None:
    branding = call("GET", "/admin/branding", admin)
    check(
        branding.status_code == 200 and branding.json()["tenant_id"] == TENANT,
        "A Tenant Administrator reads their tenant's branding",
    )
    current = branding.json()
    fields = ("display_name", "primary_color", "accent_color", "login_title", "login_message")
    change = {name: current[name] for name in (*fields, "time_zone", "locale")}
    unchanged = httpx.put(
        f"{API}/admin/branding",
        json=change,
        headers={"Authorization": f"Bearer {admin}", "Idempotency-Key": uuid.uuid4().hex},
        timeout=20,
    )
    check(
        unchanged.status_code == 401 and unchanged.json().get("code") == "step_up_required",
        "and changing it asks for a second factor (SEC-003)",
    )
    svg = httpx.put(
        f"{API}/admin/branding/logo",
        content=b"<svg onload=alert(1)>",
        headers={
            "Authorization": f"Bearer {admin}",
            "Idempotency-Key": uuid.uuid4().hex,
            "Content-Type": "image/svg+xml",
        },
        timeout=20,
    )
    check(svg.status_code == 415, "An SVG logo is refused, as it can carry scripts")


def denials(admin: str) -> list[dict[str, object]]:
    """Every denial in the tenant, a page at a time, oldest first."""
    found: list[dict[str, object]] = []
    token = ""
    while True:
        page = call(
            "GET", "/admin/access-denials", admin, params={"page_size": 200, "page_token": token}
        ).json()
        found += page["denials"]
        token = page["next_page_token"]
        if not token:
            return found


def check_denials(admin: str, operator: str, operator_id: str) -> None:
    before = {item["sequence"] for item in denials(admin)}
    refused = call("GET", "/admin/users", operator)
    check(refused.status_code == 403, "An operator cannot use the console")
    trail = call("GET", "/admin/access-denials", operator)
    check(trail.status_code == 403, "nor read the access denials, which need audit.read")
    # The record travels through the outbox, JetStream and the Audit Service, so it may lag.
    seen = False
    for _ in range(30):
        seen = any(
            item["sequence"] not in before
            and item["user_id"] == operator_id
            and item["permission"] == "users.manage"
            for item in denials(admin)
        )
        if seen:
            break
        time.sleep(2)
    check(seen, "The administrator sees the operator's refused attempt (SRS 9.4)")


def main() -> int:
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    try:
        with keycloak_admin() as base:
            with admin_client(base, admin_password) as api:
                admin_id = create_user(api, ADMIN_EMAIL, password)
                ensure_member(api, ensure_tenant(api, TENANT, TENANTS[TENANT]), admin_id)
                operator_id = str(
                    api.get(
                        f"/{REALM}/users", params={"email": demo_email(TENANT), "exact": "true"}
                    ).json()[0]["id"]
                )
            try:
                make_tenant_administrator(admin_id)
                with keycloak_public() as public:
                    realm = Realm(public)
                    admin = realm.login(ADMIN_EMAIL, password)
                    operator = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
                    if admin is None or operator is None:
                        raise RuntimeError("A check user could not sign in")
                    check_reads(admin.access, admin_id, operator_id)
                    check_step_up(admin.access, operator_id)
                    check_branding(admin.access)
                    check_denials(admin.access, operator.access, operator_id)
            finally:
                with admin_client(base, admin_password) as api:
                    delete_user(api, ADMIN_EMAIL)
    except (RuntimeError, OSError, httpx.HTTPError, KeyError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Administrator API check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
