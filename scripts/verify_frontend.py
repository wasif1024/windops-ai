#!/usr/bin/env python3
"""Sign in through a real browser, and see the branded, role-aware shell and console (SRS 4.11).

Run `make images`, `make services`, `make gateway-routes` and the seeds first. Needs local port
9443 free. The first run downloads Chromium for Playwright.

For the administrator console, it makes a temporary Tenant Administrator for each screen size,
and sets up their authenticator straight with Keycloak, as the gateway allows only 5 sign-ins a
minute from one address (SRS 7.5). The browser then signs in through the gateway. Every user it
makes is deleted afterwards. Their grants stay, for IDs that can no longer sign in (RBAC-005).
"""

import base64
import hashlib
import os
import secrets
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from identity_seed import (
    ADMIN_REQUIRED_ACTIONS,
    ADMIN_SECRET_FILE,
    PUBLIC_URL,
    TENANTS,
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
from tls_client import authority_file
from verify_identity import Realm, delete_user
from verify_mfa import Browser, enroll

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
TENANT = "north-sea"
RUN = secrets.token_hex(3)
PROJECTS = ("desktop", "phone")
ADMINS = {project: f"admin-ui-{project}-{RUN}@windops-check.example" for project in PROJECTS}
# The users each journey adds, which are deleted here afterwards.
ADDED = {project: f"added-{project}-{RUN}@windops-check.example" for project in PROJECTS}
API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"


def gateway_spki() -> str:
    """The base64 SHA-256 of the gateway certificate's public key, as Chromium pins it."""
    pem = ssl.get_server_certificate(("localhost", 9443))
    key = x509.load_pem_x509_certificate(pem.encode()).public_key()
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def make_tenant_administrator(user_id: str) -> None:
    subprocess.run(
        [
            "kubectl", "--context", "windops", "--namespace=windops-system", "exec",
            "deployment/windops-authorization", "--", "python", "-m",
            "windops_authorization.bootstrap",
            "--tenant", TENANT, "--user", user_id, "--role", "tenant-administrator",
        ],
        capture_output=True, text=True, check=True,
    )  # fmt: skip


def prepare_admins(api: httpx.Client, password: str) -> None:
    organization = ensure_tenant(api, TENANT, TENANTS[TENANT])
    for email in ADMINS.values():
        user_id = ensure_user(
            api, email, password, new_password=True, required_actions=ADMIN_REQUIRED_ACTIONS
        )
        ensure_member(api, organization, user_id)
        make_tenant_administrator(user_id)


def enroll_admins(password: str) -> tuple[dict[str, str], str]:
    """Set up each administrator's authenticator, and sign the operator in, all straight with
    Keycloak. Return each project's authenticator key, and the operator's access token."""
    keys: dict[str, str] = {}
    with keycloak_public(through_gateway=False) as public:
        realm = Realm(public)
        for project, email in ADMINS.items():
            keys[project] = enroll(Browser(realm), email, password).secret
        operator = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if operator is None:
        raise RuntimeError("The demo operator could not sign in")
    return keys, operator.access


def run_browser(password: str, keys: dict[str, str], operator_token: str) -> int:
    with keycloak_public():
        # An operator's refused attempt, so the console has an access denial to show (SRS 9.4).
        refused = httpx.get(
            f"{API}/admin/users", headers={"Authorization": f"Bearer {operator_token}"}, timeout=20
        )
        if refused.status_code != 403:
            raise RuntimeError(f"The operator was not refused: {refused.status_code}")
        env = {
            **{k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"},
            "BASE_URL": "https://localhost:9443",
            "GATEWAY_SPKI": gateway_spki(),
            "OPERATOR_EMAIL": demo_email(TENANT),
            "OPERATOR_PASSWORD": read_demo_passwords()[TENANT],
            "TENANT": TENANT,
            "ADMIN_PASSWORD": password,
            # Node's own requests trust the internal CA too, and nothing is turned off.
            "NODE_EXTRA_CA_CERTS": authority_file(),
            # Codes used while setting up count as used, so the browser waits for a newer one.
            "ENROLLED_AT": str(int(time.time())),
        }
        for project in PROJECTS:
            upper = project.upper()
            env[f"ADMIN_EMAIL_{upper}"] = ADMINS[project]
            env[f"ADMIN_TOTP_KEY_{upper}"] = keys[project]
            env[f"ADDED_EMAIL_{upper}"] = ADDED[project]
        result = subprocess.run(["npx", "playwright", "test"], cwd=FRONTEND, env=env, check=False)
    return result.returncode


def main() -> int:
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND, check=True)
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=FRONTEND, check=True)
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    code = 1
    try:
        with keycloak_admin() as base:
            try:
                with admin_client(base, admin_password) as api:
                    prepare_admins(api, password)
                keys, operator_token = enroll_admins(password)
                code = run_browser(password, keys, operator_token)
            finally:
                # A fresh login, as the first may have expired during the browser run.
                with admin_client(base, admin_password) as api:
                    for email in (*ADMINS.values(), *ADDED.values()):
                        delete_user(api, email)
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if code != 0:
        print("The browser check failed", file=sys.stderr)
        return 1
    print("Frontend check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
