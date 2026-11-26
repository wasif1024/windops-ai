#!/usr/bin/env python3
"""Sign in through a real browser, and see the shell, the administrator and Platform consoles.

Run `make images`, `make services`, `make gateway-routes` and the seeds first. Needs local port
9443 free. The first run downloads Chromium for Playwright.

For the consoles, it makes a temporary Tenant and Platform Administrator for each screen size,
and sets up their authenticator straight with Keycloak, as the gateway allows only 5 sign-ins a
minute from one address (SRS 7.5). The browser then signs in through the gateway. Every user it
makes is deleted afterwards. Their grants stay, for IDs that can no longer sign in (RBAC-005).

For the alarm console, a demo turbine's device raises a check-only alarm per screen size through
the gateway's device route, as the alarm check does, and the operator acknowledges it in the
browser. Those alarms are removed afterwards.

For the maintenance planner and parts lookup (M9.4), it makes a Maintenance Planner for each
screen size, signing in with a password alone, and a check-only part and an active case for each.
The planner finds the part and drafts and submits a work order with it. The users, parts, cases
and work orders are removed afterwards.

For the approval queue (M10.2), it makes a Maintenance Manager for each screen size, also with a
password alone, and a submitted work order for each, which the manager approves in the browser.
Their approval requests are removed afterwards.

For the investigation screens (M11.1), it makes a Reliability Engineer for each screen size, with a
password alone, who investigates the demo turbine in the browser. The run is rehearsed, at no cost.
The engineer follows it live to review, then challenges a finding. Those cases are cancelled
afterwards.
"""

import base64
import hashlib
import os
import secrets
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from scripts.checks.consoles.planner_setup import PlannerSetup
from scripts.checks.fleet.alarm import TURBINE, report, sql
from scripts.checks.fleet.telemetry import device_token, send_to
from scripts.checks.identity.identity import Realm, delete_user
from scripts.checks.identity.mfa import Browser, enroll
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import authority_file
from scripts.setup.identity_seed import (
    ADMIN_REQUIRED_ACTIONS,
    ADMIN_SECRET_FILE,
    PLATFORM_TENANT,
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
from scripts.setup.provision_postgres import read_passwords

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
TENANT = "north-sea"
RUN = secrets.token_hex(3)
PROJECTS = ("desktop", "phone")
ADMINS = {project: f"admin-ui-{project}-{RUN}@windops-check.example" for project in PROJECTS}
PLATFORM_ADMINS = {
    project: f"platform-ui-{project}-{RUN}@windops-check.example" for project in PROJECTS
}
# The users each journey adds, which are deleted here afterwards.
ADDED = {project: f"added-{project}-{RUN}@windops-check.example" for project in PROJECTS}
API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"
# The Maintenance Planner each screen size signs in as (M9.4).
PLANNERS = {project: f"planner-ui-{project}-{RUN}@windops-check.example" for project in PROJECTS}
# The Maintenance Manager each screen size signs in as (M10.2).
MANAGERS = {project: f"manager-ui-{project}-{RUN}@windops-check.example" for project in PROJECTS}
# The Reliability Engineer each screen size investigates as (M11.1), and their case's title.
ENGINEERS = {project: f"engineer-ui-{project}-{RUN}@windops-check.example" for project in PROJECTS}
INVESTIGATIONS = {project: f"Investigation screen {project} {RUN}" for project in PROJECTS}
# The alarm each screen size acknowledges. The alarm check's clean-up removes these too.
ALARM_TAGS = {project: f"alarm-check-ui-{project}-{RUN}" for project in PROJECTS}


def gateway_spki() -> str:
    """The base64 SHA-256 of the gateway certificate's public key, as Chromium pins it."""
    pem = ssl.get_server_certificate(("localhost", 9443))
    key = x509.load_pem_x509_certificate(pem.encode()).public_key()
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def bootstrap(tenant: str, user_id: str, role: str) -> None:
    subprocess.run(
        [
            "kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-system", "exec",
            "deployment/windops-authorization", "--", "python", "-m",
            "windops_authorization.bootstrap",
            "--tenant", tenant, "--user", user_id, "--role", role,
        ],
        capture_output=True, text=True, check=True,
    )  # fmt: skip


def prepare_admins(api: httpx.Client, password: str) -> None:
    for tenant, name, emails, role in (
        (TENANT, TENANTS[TENANT], ADMINS, "tenant-administrator"),
        (PLATFORM_TENANT, "WindOps Platform", PLATFORM_ADMINS, "platform-administrator"),
    ):
        organization = ensure_tenant(api, tenant, name)
        for email in emails.values():
            user_id = ensure_user(
                api, email, password, new_password=True, required_actions=ADMIN_REQUIRED_ACTIONS
            )
            ensure_member(api, organization, user_id)
            bootstrap(tenant, user_id, role)
    organization = ensure_tenant(api, TENANT, TENANTS[TENANT])
    for email in PLANNERS.values():
        user_id = ensure_user(api, email, password, new_password=True)
        ensure_member(api, organization, user_id)
        bootstrap(TENANT, user_id, "maintenance-planner")
    for email in MANAGERS.values():
        user_id = ensure_user(api, email, password, new_password=True)
        ensure_member(api, organization, user_id)
        bootstrap(TENANT, user_id, "maintenance-manager")
    for email in ENGINEERS.values():
        user_id = ensure_user(api, email, password, new_password=True)
        ensure_member(api, organization, user_id)
        bootstrap(TENANT, user_id, "reliability-engineer")


def enroll_admins(password: str) -> tuple[dict[str, str], str]:
    """Set up each administrator's authenticator, and sign the operator in, all straight with
    Keycloak. Return each one's authenticator key by email, and the operator's access token."""
    keys: dict[str, str] = {}
    with keycloak_public(through_gateway=False) as public:
        realm = Realm(public)
        for email in (*ADMINS.values(), *PLATFORM_ADMINS.values()):
            keys[email] = enroll(Browser(realm), email, password).secret
        operator = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if operator is None:
        raise RuntimeError("The demo operator could not sign in")
    return keys, operator.access


def raise_alarms() -> None:
    """A check-only alarm per screen size, from a demo turbine's device, as a controller would."""
    now = datetime.now(UTC).replace(microsecond=0)
    answer = send_to("alarms", device_token(TENANT, TURBINE),
                     [report("raise", tag, now) for tag in ALARM_TAGS.values()])  # fmt: skip
    if answer.status_code != 200 or answer.json()["accepted"] != len(ALARM_TAGS):
        raise RuntimeError(f"The device's alarms were not accepted: {answer.text}")


def remove_alarms() -> None:
    sql(
        "DELETE FROM alarm_transitions WHERE alarm_id IN (SELECT alarm_id FROM alarms "
        "WHERE tag LIKE 'alarm-check-ui-%'); DELETE FROM alarms WHERE tag LIKE 'alarm-check-ui-%'; "
        "DELETE FROM alarm_reports WHERE tag LIKE 'alarm-check-ui-%'; SELECT 1"
    )


def run_browser(
    password: str, keys: dict[str, str], operator_token: str, planning: PlannerSetup
) -> int:
    with keycloak_public():
        # An operator's refused attempt, so the console has an access denial to show (SRS 9.4).
        refused = httpx.get(
            f"{API}/admin/users", headers={"Authorization": f"Bearer {operator_token}"}, timeout=20
        )
        if refused.status_code != 403:
            raise RuntimeError(f"The operator was not refused: {refused.status_code}")
        raise_alarms()
        env = {
            **{k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"},
            "BASE_URL": PUBLIC_URL.removesuffix("/auth"),
            "GATEWAY_SPKI": gateway_spki(),
            "OPERATOR_EMAIL": demo_email(TENANT),
            "OPERATOR_PASSWORD": read_demo_passwords()[TENANT],
            "TENANT": TENANT,
            "ADMIN_PASSWORD": password,
            # Node's own requests trust the internal CA too, and nothing is turned off.
            "NODE_EXTRA_CA_CERTS": authority_file(),
            # Codes used while setting up count as used, so the browser waits for a newer one.
            "ENROLLED_AT": str(int(time.time())),
            "PLANNER_PASSWORD": password,
            "PLANNER_TURBINE": TURBINE,
        }
        for project in PROJECTS:
            upper = project.upper()
            env[f"ADMIN_EMAIL_{upper}"] = ADMINS[project]
            env[f"ADMIN_TOTP_KEY_{upper}"] = keys[ADMINS[project]]
            env[f"ADDED_EMAIL_{upper}"] = ADDED[project]
            env[f"PLATFORM_EMAIL_{upper}"] = PLATFORM_ADMINS[project]
            env[f"PLATFORM_TOTP_KEY_{upper}"] = keys[PLATFORM_ADMINS[project]]
            env[f"ALARM_TAG_{upper}"] = ALARM_TAGS[project]
            env[f"PLANNER_EMAIL_{upper}"] = PLANNERS[project]
            env[f"PLANNER_PART_{upper}"] = planning.parts[project]
            env[f"PLANNER_CASE_{upper}"] = planning.cases[project]
            env[f"MANAGER_EMAIL_{upper}"] = MANAGERS[project]
            env[f"MANAGER_WORK_ORDER_{upper}"] = planning.submitted[project]
            env[f"ENGINEER_EMAIL_{upper}"] = ENGINEERS[project]
            env[f"INVESTIGATION_TITLE_{upper}"] = INVESTIGATIONS[project]
        result = subprocess.run(["npx", "playwright", "test"], cwd=FRONTEND, env=env, check=False)
    return result.returncode


def main() -> int:
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND, check=True)
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=FRONTEND, check=True)
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    code = 1
    planning = PlannerSetup(RUN, PROJECTS)
    try:
        with keycloak_admin() as base:
            try:
                with admin_client(base, admin_password) as api:
                    prepare_admins(api, password)
                keys, operator_token = enroll_admins(password)
                planning.make()
                code = run_browser(password, keys, operator_token, planning)
            finally:
                remove_alarms()
                planning.remove()
                planning.cancel_titled(set(INVESTIGATIONS.values()))
                # A fresh login, as the first may have expired during the browser run.
                with admin_client(base, admin_password) as api:
                    for email in (
                        *ADMINS.values(),
                        *PLATFORM_ADMINS.values(),
                        *ADDED.values(),
                        *PLANNERS.values(),
                        *MANAGERS.values(),
                        *ENGINEERS.values(),
                    ):
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
