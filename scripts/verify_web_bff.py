#!/usr/bin/env python3
"""Prove Web BFF on the real cluster: real Keycloak logins, through the gateway, over HTTPS.

Run `make services`, `make gateway-routes`, `make identity-seed`, `make authorization-seed` and
`make tenant-seed` first. Needs local port 9443 free, as Keycloak's pages name that address.
"""

import subprocess
import sys
import time

import httpx
from identity_seed import PUBLIC_URL, TENANTS, demo_email, keycloak_public, read_demo_passwords
from verify_identity import Realm

CONTEXT = "windops"
DATA = "windops-data"
API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"
TENANT, OTHER = "north-sea", "atlas-wind"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> None:
    subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={DATA}", *arguments],
        capture_output=True, text=True, check=True,
    )  # fmt: skip


def get(path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(f"{API}{path}", headers=headers, timeout=15)


def sign_in(realm: Realm, tenant: str) -> str:
    tokens = realm.login(demo_email(tenant), read_demo_passwords()[tenant])
    if tokens is None:
        raise RuntimeError(f"{tenant}'s demo operator could not sign in")
    return tokens.access


def check_tokens(realm: Realm) -> str:
    missing = get("/me")
    check(missing.status_code == 401, "Without a token, the gateway refuses before the API")
    token = sign_in(realm, TENANT)
    me = get("/me", token)
    body = me.json()
    check(
        me.status_code == 200 and body["tenant_id"] == TENANT and body["client"] == "windops-web",
        f"A real Keycloak login reaches the API through the gateway, as {TENANT}",
    )
    check(me.headers.get("RateLimit-Limit") == "120", "Its standard user limit is 120 a minute")
    head, payload, signature = token.split(".")
    flipped = signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")
    check(
        get("/me", f"{head}.{payload}.{flipped}").status_code == 401,
        "A token with a changed signature is refused",
    )
    return token


def check_calls(token: str, realm: Realm) -> None:
    tenant = get("/tenant", token)
    check(
        tenant.status_code == 200 and tenant.json()["tenant_id"] == TENANT,
        "It calls the Tenant Service as that user, over mTLS",
    )
    branding = get("/branding", token)
    check(branding.json()["tenant_id"] == TENANT, "and gets that tenant's branding")
    other = sign_in(realm, OTHER)
    check(
        get("/branding", other).json()["tenant_id"] == OTHER,
        f"Another tenant's user gets {OTHER}'s, from the same address",
    )
    public = get(f"/public/branding/{TENANT}")
    check(
        public.status_code == 200 and "verification" not in public.text,
        "A sign-in page gets public branding with no token, and no domain proof token",
    )
    check(
        get("/public/branding/no-such-tenant").status_code == 404, "An unknown tenant is not found"
    )
    document = get("/openapi.json").json()
    check(document["info"]["version"] == "1.0.0", "The versioned OpenAPI document is published")


def check_limits_fail_closed(token: str) -> None:
    """SRS 7.7: with Redis down, protected routes refuse rather than run unlimited."""
    kubectl("scale", "deployment/windops-redis", "--replicas=0")
    try:
        kubectl("wait", "pod", "--selector=app.kubernetes.io/name=windops-redis",
                "--for=delete", "--timeout=120s")  # fmt: skip
        status = get("/me", token).status_code
        check(status == 503, f"With Redis down, protected routes refuse ({status})")
        check(
            get(f"/public/branding/{TENANT}").status_code == 200,
            "while public branding, which has no per-user limit, still works",
        )
    finally:
        kubectl("scale", "deployment/windops-redis", "--replicas=1")
        kubectl("rollout", "status", "deployment/windops-redis", "--timeout=180s")
    for _ in range(30):
        if get("/me", token).status_code == 200:
            break
        time.sleep(2)
    check(get("/me", token).status_code == 200, "They work again once Redis returns")


def main() -> int:
    assert TENANT in TENANTS and OTHER in TENANTS  # noqa: S101
    try:
        with keycloak_public() as public:
            realm = Realm(public)
            token = check_tokens(realm)
            check_calls(token, realm)
            check_limits_fail_closed(token)
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Web BFF check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
