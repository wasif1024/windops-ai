#!/usr/bin/env python3
"""Prove the Security dashboard and its alerts have data behind them, from real refusals (SRS 10.3).

Run `make infra`, `make services`, `make gateway-routes` and the seeds first. Needs local port
9443 free. It makes a temporary user, causes one of each refusal the dashboard counts, then asks
Prometheus for every panel's query, and Grafana for the dashboard. The user is deleted afterwards.
It uses the gateway's sign-in limit, so it waits a minute at the end for it to clear.
"""

import base64
import json
import secrets
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from gateway_routes import port_forward
from identity_seed import (
    ADMIN_REQUIRED_ACTIONS,
    ADMIN_SECRET_FILE,
    CLIENT_ID,
    PUBLIC_URL,
    REALM,
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
from settings import KUBE_CONTEXT
from tls_client import authority_file
from verify_identity import Realm, delete_user
from verify_mfa import Browser, enroll, has_field, sign_in_with_password, wrong_code
from windops_auth import LEVEL_MFA

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "infrastructure" / "monitoring" / "grafana" / "dashboards" / "security.json"
OBSERVABILITY = "windops-observability"
SYSTEM = "windops-system"
TENANT = "north-sea"
API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"
USER = f"security-check-{secrets.token_hex(3)}@windops-check.example"
BFF_HOST = "windops-web-bff.windops-system.svc.cluster.local"
SECURITY_ALERTS = {
    "WindopsManyFailedSignIns",
    "WindopsManyAccessDenials",
    "WindopsSustainedRateLimiting",
    "WindopsBreakGlassStarted",
    "WindopsAdministratorRoleGranted",
}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> str:
    return subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, *arguments],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def make_tenant_administrator(user_id: str) -> None:
    """Also an administrator event, which the dashboard lists and an alert reports."""
    kubectl(
        "exec", "deployment/windops-authorization", f"--namespace={SYSTEM}", "--",
        "python", "-m", "windops_authorization.bootstrap",
        "--tenant", TENANT, "--user", user_id, "--role", "tenant-administrator",
    )  # fmt: skip


def sign_in_failures(password: str) -> str:
    """A wrong password and a wrong code, straight with Keycloak, and the operator's token."""
    with keycloak_public(through_gateway=False) as public:
        realm = Realm(public)
        enrolled = enroll(Browser(realm), USER, password)
        # Twice: Prometheus cannot see the first step of a series Keycloak only now starts.
        # Seconds apart, as Keycloak locks out failures that come quicker than a person types.
        for _ in range(2):
            wrong = Browser(realm)
            wrong.start(CLIENT_ID)
            sign_in_with_password(wrong, USER, password + "x")
            time.sleep(3)
        browser = Browser(realm)
        browser.start(CLIENT_ID, acr_values=LEVEL_MFA)
        page = sign_in_with_password(browser, USER, password)
        if not has_field(page, "otp"):
            raise RuntimeError("Keycloak did not ask for a code")
        browser.submit({"otp": wrong_code(enrolled.secret)})
        operator = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if operator is None:
        raise RuntimeError("The demo operator could not sign in")
    check(True, "A wrong password and a wrong authenticator code were refused by Keycloak")
    return operator.access


def edge_refusals(operator: str) -> None:
    with keycloak_public():
        denied = httpx.get(f"{API}/admin/users", headers={"Authorization": f"Bearer {operator}"})
        check(denied.status_code == 403, "An operator was refused the console: an access denial")
        bearer = {"Authorization": f"Bearer {operator}"}
        codes = [httpx.get(f"{API}/me", headers=bearer, timeout=20).status_code for _ in range(125)]
        check(429 in codes, "Web BFF refused requests over the operator's per-user limit")
        url = f"{PUBLIC_URL}/realms/{REALM}/login-actions/authenticate"
        posts = [httpx.post(url, data={"username": "x"}).status_code for _ in range(7)]
        check(429 in posts, "The gateway refused sign-in attempts over its per-address limit")
    context = ssl.create_default_context(cafile=authority_file())
    with (
        port_forward("deployment/windops-web-bff", SYSTEM, 8443) as port,
        httpx.Client(verify=context) as client,
    ):
        # The certificate names the service, not the local forward.
        refused = client.request(
            "GET",
            f"https://127.0.0.1:{port}/api/v1/me",
            headers={"Authorization": "Bearer not.a.token"},
            extensions={"sni_hostname": BFF_HOST},
        )
    check(refused.status_code == 401, "Web BFF refused a forged token that reached it directly")


def query(base: str, expr: str) -> list[dict[str, Any]]:
    answer = httpx.get(f"{base}/api/v1/query", params={"query": expr}, timeout=20).json()
    if answer.get("status") != "success":
        raise RuntimeError(f"Prometheus refused a panel's query: {expr}")
    result: list[dict[str, Any]] = answer["data"]["result"]
    return result


def has_data(result: list[dict[str, Any]]) -> bool:
    return any(float(item["value"][1]) > 0 for item in result)


def check_panels(base: str) -> None:
    panels = [
        panel for panel in json.loads(DASHBOARD.read_text())["panels"] if panel.get("targets")
    ]
    pending = {panel["title"]: panel["targets"][0]["expr"] for panel in panels}
    # Scraped every 30 seconds, and the audit record travels through the outbox first.
    for _ in range(24):
        pending = {
            title: expr for title, expr in pending.items() if not has_data(query(base, expr))
        }
        if not pending:
            break
        time.sleep(5)
    for title in (panel["title"] for panel in panels):
        check(title not in pending, f"The panel '{title}' shows it")


def check_alerts(base: str) -> None:
    groups = httpx.get(f"{base}/api/v1/rules", timeout=20).json()["data"]["groups"]
    loaded = {rule["name"] for group in groups for rule in group["rules"]}
    check(loaded >= SECURITY_ALERTS, "Prometheus has every security alert")
    firing: set[str] = set()
    for _ in range(12):
        alerts = httpx.get(f"{base}/api/v1/alerts", timeout=20).json()["data"]["alerts"]
        firing = {alert["labels"]["alertname"] for alert in alerts}
        if {"WindopsAdministratorRoleGranted"} <= firing:
            break
        time.sleep(10)
    check(
        "WindopsAdministratorRoleGranted" in firing,
        "Granting an administrator role raised its alert",
    )


def check_grafana() -> None:
    encoded = kubectl(
        "get", "secret", "windops-grafana-admin", f"--namespace={OBSERVABILITY}",
        "--output=jsonpath={.data.admin-password}",
    )  # fmt: skip
    password = base64.b64decode(encoded).decode()
    with port_forward("service/windops-grafana", OBSERVABILITY, 3000) as port:
        board = httpx.get(
            f"http://127.0.0.1:{port}/api/dashboards/uid/windops-security",
            auth=("admin", password),
            timeout=20,
        )
    check(
        board.status_code == 200 and board.json()["dashboard"]["title"] == "Security",
        "Grafana has the Security dashboard",
    )


def main() -> int:
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    try:
        with keycloak_admin() as keycloak:
            try:
                with admin_client(keycloak, admin_password) as api:
                    user_id = ensure_user(
                        api, USER, password, new_password=True,
                        required_actions=ADMIN_REQUIRED_ACTIONS,
                    )  # fmt: skip
                    ensure_member(api, ensure_tenant(api, TENANT, TENANTS[TENANT]), user_id)
                make_tenant_administrator(user_id)
                operator = sign_in_failures(password)
                edge_refusals(operator)
                with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as port:
                    base = f"http://127.0.0.1:{port}"
                    check_panels(base)
                    check_alerts(base)
                check_grafana()
            finally:
                with admin_client(keycloak, admin_password) as api:
                    delete_user(api, USER)
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Waiting a minute, so the sign-in limit does not slow the next check", flush=True)
    time.sleep(61)
    print("Security check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
