#!/usr/bin/env python3
"""Prove the Platform console's API on the real cluster, through the gateway (M3.10, RBAC-003).

Run `make services`, `make gateway-routes`, `make identity-seed`, `make authorization-seed` and
`make tenant-seed` first. Needs local port 9443 free, as Keycloak's pages name that address.

It makes a temporary Platform Administrator, sets up their authenticator and signs them in with
it straight with Keycloak, as the gateway allows 5 sign-ins a minute. Their token then goes
through the gateway. It creates the tenant `platform-check` once, and suspends and reactivates
it each run. The user is deleted afterwards. Their grant stays, for an ID that can no longer
sign in (RBAC-005).
"""

import secrets
import subprocess
import sys
import uuid

import httpx
from identity_seed import (
    ADMIN_REQUIRED_ACTIONS,
    ADMIN_SECRET_FILE,
    CLIENT_ID,
    PLATFORM_TENANT,
    PUBLIC_URL,
    admin_client,
    demo_email,
    ensure_member,
    ensure_tenant,
    ensure_user,
    keycloak_admin,
    keycloak_public,
    read_demo_passwords,
    strong_password,
)
from provision_postgres import read_passwords
from verify_identity import Realm, delete_user
from verify_mfa import Browser, enroll, has_field, sign_in_with_password
from windops_auth import LEVEL_MFA

API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"
ADMIN_EMAIL = f"platform-api-{secrets.token_hex(3)}@windops-check.example"
CHECK_TENANT = "platform-check"
REASON = "Platform API check, proving break-glass end to end"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def call(
    method: str, path: str, token: str, *, json: object | None = None, key: bool = True
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if method != "GET" and key:
        headers["Idempotency-Key"] = uuid.uuid4().hex
    return httpx.request(method, f"{API}{path}", headers=headers, json=json, timeout=30)


def make_platform_administrator(user_id: str) -> None:
    subprocess.run(
        [
            "kubectl", "--context", "windops", "--namespace=windops-system", "exec",
            "deployment/windops-authorization", "--", "python", "-m",
            "windops_authorization.bootstrap",
            "--tenant", PLATFORM_TENANT, "--user", user_id, "--role", "platform-administrator",
        ],
        capture_output=True, text=True, check=True,
    )  # fmt: skip


def sign_in(password: str) -> tuple[str, str]:
    """Return the Platform Administrator's token at the mfa level, and the operator's."""
    with keycloak_public(through_gateway=False) as public:
        realm = Realm(public)
        enrolled = enroll(Browser(realm), ADMIN_EMAIL, password)
        browser = Browser(realm)
        browser.start(CLIENT_ID, acr_values=LEVEL_MFA)
        page = sign_in_with_password(browser, ADMIN_EMAIL, password)
        if not has_field(page, "otp"):
            raise RuntimeError("Keycloak did not ask for a code")
        browser.submit({"otp": enrolled.code()})
        operator = realm.login(demo_email("north-sea"), read_demo_passwords()["north-sea"])
        if operator is None:
            raise RuntimeError("The demo operator could not sign in")
        return browser.tokens()["access_token"], operator.access


def check_tenants(admin: str, operator: str) -> None:
    listed = call("GET", "/platform/tenants", admin)
    ids = {item["tenant_id"] for item in listed.json().get("tenants", [])}
    check(
        listed.status_code == 200 and {"north-sea", "atlas-wind"} <= ids,
        "A Platform Administrator lists every tenant",
    )
    refused = call("GET", "/platform/tenants", operator)
    check(refused.status_code == 403, "A tenant's user cannot")
    created = call(
        "POST",
        "/platform/tenants",
        admin,
        json={"tenant_id": CHECK_TENANT, "name": "Platform Check"},
    )
    check(
        created.status_code == 201 and created.json()["tenant"]["tenant_id"] == CHECK_TENANT,
        f"creates a tenant, or finds it from before (created={created.json().get('created')})",
    )
    unkeyed = call("POST", f"/platform/tenants/{CHECK_TENANT}/reactivate", admin, key=False)
    check(unkeyed.status_code == 422, "A change with no Idempotency-Key is refused (GRP-007)")
    suspended = call(
        "POST",
        f"/platform/tenants/{CHECK_TENANT}/suspend",
        admin,
        json={"reason": "Platform API check, suspending for a moment"},
    )
    check(
        suspended.status_code == 200 and suspended.json()["status"] == "suspended",
        "suspends it",
    )
    back = call("POST", f"/platform/tenants/{CHECK_TENANT}/reactivate", admin)
    check(back.status_code == 200 and back.json()["status"] == "active", "and reactivates it")


def check_branding(admin: str) -> None:
    branding = call("GET", "/platform/tenants/north-sea/branding", admin)
    body = branding.json()
    check(
        branding.status_code == 200 and body["tenant_id"] == "north-sea",
        "sees a tenant's branding",
    )
    domain = body.get("custom_domain") or {}
    check("verification_token" not in domain, "without its custom domain's proof token")
    # Branding is the tenant's own to change (SRS 3.2), so there is no route for it here.
    change = call("PUT", "/platform/tenants/north-sea/branding", admin, json={"display_name": "x"})
    check(change.status_code == 405, "and has no way to change it")


def check_break_glass(admin: str) -> None:
    started = call(
        "POST",
        "/platform/break-glass",
        admin,
        json={"tenant_id": "north-sea", "reason": REASON, "duration_minutes": 5},
    )
    check(started.status_code == 201, "starts reasoned, time-limited break-glass access (RBAC-003)")
    grant = started.json()["grant_id"]
    listed = call("GET", "/platform/break-glass", admin).json()["grants"]
    check(
        [(item["grant_id"], item["tenant_id"]) for item in listed] == [(grant, "north-sea")],
        "sees it among their own active grants",
    )
    ended = call("POST", f"/platform/break-glass/{grant}/end", admin)
    check(ended.status_code == 200 and ended.json()["ended_at"], "ends it early")
    check(not call("GET", "/platform/break-glass", admin).json()["grants"], "and it is gone")


def main() -> int:
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    try:
        with keycloak_admin() as base:
            try:
                with admin_client(base, admin_password) as api:
                    user_id = ensure_user(
                        api,
                        ADMIN_EMAIL,
                        password,
                        new_password=True,
                        required_actions=ADMIN_REQUIRED_ACTIONS,
                    )
                    ensure_member(
                        api, ensure_tenant(api, PLATFORM_TENANT, "WindOps Platform"), user_id
                    )
                make_platform_administrator(user_id)
                admin, operator = sign_in(password)
                with keycloak_public():
                    check_tenants(admin, operator)
                    check_branding(admin)
                    check_break_glass(admin)
            finally:
                with admin_client(base, admin_password) as api:
                    delete_user(api, ADMIN_EMAIL)
    except (
        RuntimeError,
        OSError,
        httpx.HTTPError,
        KeyError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Platform API check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
