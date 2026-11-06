#!/usr/bin/env python3
"""Call every Web BFF route as three callers, and require each to fail safely (Phase 3 gate).

The routes come from the published OpenAPI document, so a new route fails the gate until it is
given a class here. Run as part of `make phase-3-gate`, or on its own with `make route-sweep`.

Callers: no token; a Control Room Operator, who holds no administrative permission; and a Tenant
Administrator signed in with a password only, so with no recent second factor.
"""

import json
import secrets
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

ROOT = Path(__file__).resolve().parent.parent
OPENAPI = ROOT / "docs" / "api" / "web-bff-v1.openapi.json"
BASE = PUBLIC_URL.removesuffix("/auth")
TENANT = "north-sea"
# Every other {tenant_id} names another tenant, which only the Platform Administrator may reach.
OTHER_TENANT = "atlas-wind"
ADMIN_EMAIL = f"route-sweep-{secrets.token_hex(3)}@windops-check.example"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# Each route's class. Public needs no sign-in. Signed-in is for anyone in a tenant. Member reads
# are console reads any member may make, as nothing in them is sensitive. Admin reads need an
# administrative permission. Admin changes need one and a recent second factor. Platform is for
# the Platform Administrator only.
PUBLIC, SIGNED_IN, MEMBER_READ, ADMIN_READ, ADMIN_CHANGE, PLATFORM = (
    "public", "signed-in", "member-read", "admin-read", "admin-change", "platform",
)  # fmt: skip
CLASSES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/public/branding/{tenant_id}"): PUBLIC,
    ("GET", "/api/v1/public/branding/{tenant_id}/logo"): PUBLIC,
    ("GET", "/api/v1/me"): SIGNED_IN,
    ("GET", "/api/v1/me/permissions"): SIGNED_IN,
    ("GET", "/api/v1/branding"): SIGNED_IN,
    ("GET", "/api/v1/tenant"): SIGNED_IN,
    ("GET", "/api/v1/admin/access-denials"): ADMIN_READ,
    ("GET", "/api/v1/admin/branding"): ADMIN_READ,
    # The emails' standard wording, which the Notification Service and every member may read.
    ("GET", "/api/v1/admin/email-templates"): MEMBER_READ,
    ("GET", "/api/v1/admin/role-grants"): ADMIN_READ,
    ("GET", "/api/v1/admin/roles/{role}/permissions"): ADMIN_READ,
    ("GET", "/api/v1/admin/users"): ADMIN_READ,
    ("GET", "/api/v1/admin/users/{member_id}"): ADMIN_READ,
    ("GET", "/api/v1/admin/users/{member_id}/mfa"): ADMIN_READ,
    ("GET", "/api/v1/admin/users/{member_id}/sessions"): ADMIN_READ,
    ("PUT", "/api/v1/admin/branding"): ADMIN_CHANGE,
    ("PUT", "/api/v1/admin/branding/logo"): ADMIN_CHANGE,
    ("PUT", "/api/v1/admin/custom-domain"): ADMIN_CHANGE,
    ("DELETE", "/api/v1/admin/custom-domain"): ADMIN_CHANGE,
    ("PUT", "/api/v1/admin/email-templates/{template}"): ADMIN_CHANGE,
    ("POST", "/api/v1/admin/role-grants"): ADMIN_CHANGE,
    ("POST", "/api/v1/admin/role-grants/{grant_id}/revoke"): ADMIN_CHANGE,
    ("PUT", "/api/v1/admin/roles/{role}/permissions"): ADMIN_CHANGE,
    ("POST", "/api/v1/admin/users"): ADMIN_CHANGE,
    ("POST", "/api/v1/admin/users/{member_id}/disable"): ADMIN_CHANGE,
    ("POST", "/api/v1/admin/users/{member_id}/reactivate"): ADMIN_CHANGE,
    ("POST", "/api/v1/admin/users/{member_id}/sessions/revoke"): ADMIN_CHANGE,
    ("GET", "/api/v1/platform/break-glass"): PLATFORM,
    ("POST", "/api/v1/platform/break-glass"): PLATFORM,
    ("POST", "/api/v1/platform/break-glass/{grant_id}/end"): PLATFORM,
    ("GET", "/api/v1/platform/tenants"): PLATFORM,
    ("POST", "/api/v1/platform/tenants"): PLATFORM,
    ("GET", "/api/v1/platform/tenants/{tenant_id}"): PLATFORM,
    ("GET", "/api/v1/platform/tenants/{tenant_id}/branding"): PLATFORM,
    ("POST", "/api/v1/platform/tenants/{tenant_id}/suspend"): PLATFORM,
    ("POST", "/api/v1/platform/tenants/{tenant_id}/reactivate"): PLATFORM,
}

# A valid body for each change, so a refusal comes from permission or MFA, not validation.
BRANDING = {
    "display_name": "x", "primary_color": "#0b5394", "accent_color": "#38761d",
    "login_title": "x", "login_message": "x", "time_zone": "Europe/Oslo", "locale": "en-GB",
}  # fmt: skip
BODIES: dict[tuple[str, str], Any] = {
    ("PUT", "/api/v1/admin/branding"): BRANDING,
    ("PUT", "/api/v1/admin/custom-domain"): {"domain": "sweep.windops-check.example"},
    ("PUT", "/api/v1/admin/email-templates/{template}"): {"subject": "x", "body": "x"},
    ("POST", "/api/v1/admin/role-grants"): {"user_id": "{member_id}", "role": "auditor"},
    ("PUT", "/api/v1/admin/roles/{role}/permissions"): {"permissions": ["audit.read"]},
    ("POST", "/api/v1/admin/users"): {"email": "sweep@windops-check.example"},
    ("POST", "/api/v1/platform/break-glass"): {
        "tenant_id": OTHER_TENANT,
        "reason": "Route sweep, checking that this is refused",
    },
    ("POST", "/api/v1/platform/tenants"): {"tenant_id": "platform-check", "name": "Platform Check"},
    ("POST", "/api/v1/platform/tenants/{tenant_id}/suspend"): {"reason": "Route sweep, refused"},
}


Expected = dict[str, tuple[int, str | None]]
OK: tuple[int, str | None] = (200, None)
FORBIDDEN: tuple[int, str | None] = (403, "forbidden")
SIGN_IN: tuple[int, str | None] = (401, None)
# What each caller must get from each class of route, as (status, problem code or None for any).
NO_TOKEN: Expected = {
    PUBLIC: OK, SIGNED_IN: SIGN_IN, MEMBER_READ: SIGN_IN, ADMIN_READ: SIGN_IN,
    ADMIN_CHANGE: SIGN_IN, PLATFORM: SIGN_IN,
}  # fmt: skip
# No administrative permission: refused everything administrative, never told more.
OPERATOR: Expected = {
    PUBLIC: OK, SIGNED_IN: OK, MEMBER_READ: OK, ADMIN_READ: FORBIDDEN,
    ADMIN_CHANGE: FORBIDDEN, PLATFORM: FORBIDDEN,
}  # fmt: skip
# A password but no recent second factor: reads, but every change asks for one (SEC-003).
ADMIN: Expected = {
    PUBLIC: OK, SIGNED_IN: OK, MEMBER_READ: OK, ADMIN_READ: OK,
    ADMIN_CHANGE: (401, "step_up_required"), PLATFORM: FORBIDDEN,
}  # fmt: skip


@dataclass
class Caller:
    name: str
    token: str | None
    # What each class must answer, as (status, problem code or None for any).
    expected: Expected


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def routes() -> list[tuple[str, str]]:
    document = json.loads(OPENAPI.read_text())
    return sorted(
        (method.upper(), path) for path, item in document["paths"].items() for method in item
    )


def fill(text: str, member_id: str, tenant: str = OTHER_TENANT) -> str:
    return (
        text.replace("{member_id}", member_id)
        .replace("{tenant_id}", tenant)
        .replace("{role}", "auditor")
        .replace("{template}", "invitation")
        .replace("{grant_id}", "sweep-grant")
    )


def call(method: str, path: str, token: str | None, member_id: str) -> httpx.Response:
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # A public route names the callers' own tenant, which has a logo to serve.
    tenant = TENANT if path.startswith("/api/v1/public/") else OTHER_TENANT
    url = BASE + fill(path, member_id, tenant)
    if path.endswith("/branding/logo") and method == "PUT":
        return httpx.put(url, content=PNG, headers={**headers, "Content-Type": "image/png"})
    body = BODIES.get((method, path))
    if body is not None:
        body = json.loads(fill(json.dumps(body), member_id))
    return httpx.request(method, url, json=body, headers=headers, timeout=30)


def sweep(callers: list[Caller], member_id: str) -> int:
    failures = []
    for method, path in routes():
        kind = CLASSES[(method, path)]
        for caller in callers:
            status, code = caller.expected[kind]
            answer = call(method, path, caller.token, member_id)
            got_code = None
            if answer.headers.get("content-type") == "application/problem+json":
                got_code = answer.json().get("code")
            if answer.status_code != status or (code is not None and got_code != code):
                failures.append(
                    f"{method} {path} as {caller.name}: {answer.status_code} {got_code}, "
                    f"expected {status} {code}"
                )
    for failure in failures:
        print(f"not ok - {failure}", flush=True)
    return len(failures)


def main() -> int:
    unclassified = [route for route in routes() if route not in CLASSES]
    if unclassified:
        print(f"Give these routes a class in route_sweep.py: {unclassified}", file=sys.stderr)
        return 1
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    try:
        with keycloak_admin() as base:
            try:
                with admin_client(base, admin_password) as api:
                    admin_id = create_user(api, ADMIN_EMAIL, password)
                    ensure_member(api, ensure_tenant(api, TENANT, TENANTS[TENANT]), admin_id)
                    operator_id = str(
                        api.get(
                            f"/{REALM}/users", params={"email": demo_email(TENANT), "exact": "true"}
                        ).json()[0]["id"]
                    )
                subprocess.run(
                    [
                        "kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-system",
                        "exec", "deployment/windops-authorization", "--", "python", "-m",
                        "windops_authorization.bootstrap", "--tenant", TENANT,
                        "--user", admin_id, "--role", "tenant-administrator",
                    ],
                    capture_output=True, text=True, check=True,
                )  # fmt: skip
                with keycloak_public(through_gateway=False) as public:
                    realm = Realm(public)
                    admin = realm.login(ADMIN_EMAIL, password)
                    operator = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
                if admin is None or operator is None:
                    raise RuntimeError("A sweep caller could not sign in")
                callers = [
                    Caller("no token", None, NO_TOKEN),
                    Caller("an operator", operator.access, OPERATOR),
                    Caller("a Tenant Administrator without a second factor", admin.access, ADMIN),
                ]
                with keycloak_public():
                    failed = sweep(callers, operator_id)
                    still = httpx.get(
                        f"{BASE}/api/v1/admin/users/{operator_id}",
                        headers={"Authorization": f"Bearer {admin.access}"},
                    ).json()
                count = len(routes()) * len(callers)
                check(failed == 0, f"All {count} calls to {len(routes())} routes failed safely")
                check(still.get("enabled") is True, "and none of the refused changes happened")
            finally:
                with admin_client(base, admin_password) as api:
                    delete_user(api, ADMIN_EMAIL)
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Route sweep passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
