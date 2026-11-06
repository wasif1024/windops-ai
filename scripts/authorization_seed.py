#!/usr/bin/env python3
"""Give the demo users their first roles, which nobody could otherwise grant. Safe to repeat.

Run `make identity-seed` and `make services` first. Each tenant's demo administrator becomes its
Tenant Administrator, its demo operator a Control Room Operator, and the platform's demo
administrator the Platform Administrator.
"""

import subprocess
import sys

import httpx
from identity_seed import (
    ADMIN_SECRET_FILE,
    PLATFORM_ADMIN_EMAIL,
    PLATFORM_TENANT,
    REALM,
    TENANTS,
    admin_client,
    demo_admin_email,
    demo_email,
    keycloak_admin,
)
from provision_postgres import read_passwords
from settings import KUBE_CONTEXT

CONTEXT = KUBE_CONTEXT
NAMESPACE = "windops-system"
DEPLOYMENT = "deployment/windops-authorization"


def demo_roles() -> list[tuple[str, str, str]]:
    """Return (tenant, email, role) for each demo user."""
    roles = [(PLATFORM_TENANT, PLATFORM_ADMIN_EMAIL, "platform-administrator")]
    for alias in TENANTS:
        roles.append((alias, demo_admin_email(alias), "tenant-administrator"))
        roles.append((alias, demo_email(alias), "control-room-operator"))
    return roles


def user_id(api: httpx.Client, email: str) -> str:
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    if not found.json():
        raise RuntimeError(f"No such user: {email}. Run: make identity-seed")
    return str(found.json()[0]["id"])


def bootstrap(tenant: str, user: str, role: str) -> str:
    result = subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "exec", DEPLOYMENT, f"--namespace={NAMESPACE}",
            "--", "python", "-m", "windops_authorization.bootstrap",
            "--tenant", tenant, "--user", user, "--role", role,
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"Could not grant {role} in {tenant}: {result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    password = read_passwords(ADMIN_SECRET_FILE)["password"]
    try:
        with (
            keycloak_admin() as base,
            admin_client(base, password) as api,
        ):
            wanted = [(t, user_id(api, email), role, email) for t, email, role in demo_roles()]
        for tenant, user, role, email in wanted:
            print(f"{email} is {role} in {tenant}: {bootstrap(tenant, user, role)}")
    except (RuntimeError, httpx.HTTPError, OSError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
