#!/usr/bin/env python3
"""Prove the gateway's policy on the real cluster: token checks, CORS and per-address limits.

Run `make services`, `make gateway-routes` and the seeds first. Needs local port 9443 free. It uses
the gateway's login limit for this address, so it waits a minute at the end for it to clear.
"""

import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from gateway_routes import port_forward
from identity_seed import (
    ADMIN_SECRET_FILE,
    PUBLIC_URL,
    REALM,
    admin_client,
    demo_email,
    keycloak_admin,
    keycloak_public,
    read_demo_passwords,
    strong_password,
)
from provision_postgres import read_passwords
from verify_identity import Realm, create_user, delete_user

SYSTEM = "windops-system"
API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"
ALLOWED_ORIGIN = "http://localhost:5173"
LOGIN_LIMIT = 5
NO_TENANT_USER = f"gateway-policy-{secrets.token_hex(3)}@windops-check.example"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def gateway_answered(response: httpx.Response) -> bool:
    """Web BFF answers with RFC 9457 problems, so anything else came from the gateway."""
    return str(response.headers.get("content-type", "")) != "application/problem+json"


@contextmanager
def tenantless_user() -> Iterator[str]:
    """A user in no organization, like one whose tenant is suspended, who still signs in."""
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    with keycloak_admin() as base:
        with admin_client(base, admin_password) as api:
            create_user(api, NO_TENANT_USER, password)
        try:
            yield password
        finally:
            with admin_client(base, admin_password) as api:
                delete_user(api, NO_TENANT_USER)


def check_tokens(realm: Realm) -> None:
    tokens = realm.login(demo_email("north-sea"), read_demo_passwords()["north-sea"])
    if tokens is None:
        raise RuntimeError("The demo operator could not sign in")
    allowed = httpx.get(f"{API}/me", headers={"Authorization": f"Bearer {tokens.access}"})
    check(allowed.status_code == 200, "A valid token passes the gateway and reaches Web BFF")
    missing = httpx.get(f"{API}/me")
    check(
        missing.status_code == 401 and gateway_answered(missing),
        "With no token, the gateway refuses, and Web BFF never sees the request",
    )
    forged = httpx.get(f"{API}/me", headers={"Authorization": "Bearer not.a.token"})
    check(forged.status_code == 401 and gateway_answered(forged), "A forged token is refused too")


def check_tenantless(realm: Realm) -> None:
    with tenantless_user() as password:
        tokens = realm.login(NO_TENANT_USER, password)
        if tokens is None:
            raise RuntimeError("The user with no tenant could not sign in")
        refused = httpx.get(f"{API}/me", headers={"Authorization": f"Bearer {tokens.access}"})
        check(
            refused.status_code == 401 and gateway_answered(refused),
            "A real token with no tenant, like a suspended tenant's user has, is refused",
        )


def check_cors() -> None:
    def preflight(origin: str) -> httpx.Response:
        return httpx.options(
            f"{API}/me",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    allowed = preflight(ALLOWED_ORIGIN)
    check(
        allowed.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN
        and "Access-Control-Allow-Credentials" not in allowed.headers,
        "CORS allows the frontend's origin, without cookies",
    )
    refused = preflight("https://attacker.example")
    check(
        refused.headers.get("Access-Control-Allow-Origin") not in ("https://attacker.example", "*"),
        "and gives no other origin access (API-005)",
    )


def check_public() -> None:
    public = httpx.get(f"{API}/public/branding/north-sea")
    check(public.status_code == 200, "A sign-in page's public branding needs no token")


def check_login_limit() -> None:
    """SRS 7.5: anonymous login, 5 a minute from one address. Earlier sign-ins count too."""
    url = f"{PUBLIC_URL}/realms/{REALM}/login-actions/authenticate"
    codes = [httpx.post(url, data={"username": "x", "password": "y"}).status_code for _ in range(7)]
    check(429 in codes, f"Sign-in attempts past {LOGIN_LIMIT} a minute are refused ({codes})")


def check_bff_plain_port() -> None:
    with port_forward("deployment/windops-web-bff", SYSTEM, 8080) as port:
        plain = httpx.get(f"http://127.0.0.1:{port}/api/v1/me")
        health = httpx.get(f"http://127.0.0.1:{port}/health/live")
    check(
        plain.status_code == 404 and health.status_code == 200,
        "Web BFF's plain port has health and metrics only, and its API needs TLS",
    )


def main() -> int:
    try:
        with keycloak_public() as public:
            realm = Realm(public)
            check_tokens(realm)
            check_tenantless(realm)
            check_cors()
            check_public()
            check_login_limit()
        check_bff_plain_port()
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Waiting a minute, so the login limit does not slow the next check", flush=True)
    time.sleep(61)
    print("Gateway policy check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
