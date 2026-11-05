#!/usr/bin/env python3
"""Prove Keycloak works on the real local cluster, through real logins.

Run `make secrets`, `make infra` and `make identity-seed` first. This restarts Keycloak once, and
removes its temporary users afterwards. It also answers SRS 13's vendor check on the tenant model.
"""

import base64
import hashlib
import html
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from gateway_routes import port_forward
from identity_seed import (
    ADMIN_SECRET_FILE,
    API_AUDIENCE,
    CLIENT_ID,
    CONTEXT,
    DEPLOYMENT,
    NAMESPACE,
    POD_SELECTOR,
    REALM,
    SERVICE,
    TENANT_CLAIM,
    TENANTS,
    admin_client,
    demo_email,
    read_demo_passwords,
    strong_password,
)
from provision_postgres import ADMIN_USER as POSTGRES_ADMIN
from provision_postgres import NAMESPACE as DATA
from provision_postgres import POD as POSTGRES_POD
from provision_postgres import read_passwords

REDIRECT_URI = "http://localhost:5173/callback"
ACCESS_TOKEN_SECONDS = 300
FAILED_LOGINS_BEFORE_LOCKOUT = 5
NO_TENANT_USER = "no-tenant-check@windops-check.example"
LOCKOUT_USER = "lockout-check@windops-check.example"
TWO_TENANTS_USER = "two-tenants-check@windops-check.example"
POLICY_CHECK_USER = "policy-check@windops-check.example"
TEMPORARY_USERS = (NO_TENANT_USER, LOCKOUT_USER, POLICY_CHECK_USER, TWO_TENANTS_USER)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class Realm:
    """The realm's public endpoints, used the way a browser and client would use them."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.issuer = f"{base}/realms/{REALM}"
        self.token_url = f"{self.issuer}/protocol/openid-connect/token"
        self.auth_url = f"{self.issuer}/protocol/openid-connect/auth"

    def authorization_code(self, email: str, password: str, challenge: str) -> str | None:
        """Sign in through the login pages, and return the code, or None if login was refused."""
        with httpx.Client(follow_redirects=True, timeout=30) as browser:
            page = browser.get(
                self.auth_url,
                params={
                    "client_id": CLIENT_ID,
                    "response_type": "code",
                    "scope": "openid",
                    "redirect_uri": REDIRECT_URI,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
            )
            page.raise_for_status()
            cookies = session_cookies(page)
            # With Organizations on, the address comes first, and the password on the next page.
            answer = submit(browser, page, {"username": email}, cookies)
            if answer.status_code == 200 and 'name="password"' in answer.text:
                cookies = {**cookies, **session_cookies(answer)}
                answer = submit(
                    browser, answer, {"password": password, "credentialId": ""}, cookies
                )
            if answer.status_code not in {302, 303}:
                return None
            query = parse_qs(urlparse(answer.headers["Location"]).query)
            return query["code"][0] if "code" in query else None

    def login(self, email: str, password: str) -> Tokens | None:
        verifier, challenge = pkce_pair()
        code = self.authorization_code(email, password, challenge)
        if code is None:
            return None
        response = httpx.post(
            self.token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        return Tokens(body["access_token"], body["refresh_token"])

    def refresh(self, token: str) -> httpx.Response:
        return httpx.post(
            self.token_url,
            data={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": token},
            timeout=30,
        )

    def claims(self, token: str) -> dict[str, Any]:
        """Verify a token as a service must: signature, issuer, audience and expiry."""
        keys = httpx.get(f"{self.issuer}/protocol/openid-connect/certs", timeout=30).json()
        kid = jwt.get_unverified_header(token)["kid"]
        key = next(item for item in keys["keys"] if item["kid"] == kid)
        verified: dict[str, Any] = jwt.decode(
            token,
            jwt.PyJWK(key).key,
            algorithms=["RS256"],
            audience=API_AUDIENCE,
            issuer=self.issuer,
        )
        return verified


def session_cookies(response: httpx.Response) -> dict[str, str]:
    """Return the cookies a response set, including those from any redirects before it."""
    found: dict[str, str] = {}
    for item in [*response.history, response]:
        for header in item.headers.get_list("set-cookie"):
            name, _, value = header.split(";", 1)[0].partition("=")
            found[name.strip()] = value
    return found


def submit(
    browser: httpx.Client, page: httpx.Response, fields: dict[str, str], cookies: dict[str, str]
) -> httpx.Response:
    """Post a login form. Keycloak's cookies are marked Secure, which browsers accept on localhost
    but this client does not, so they are sent by hand until M3.7 adds TLS."""
    action = re.search(r'action="([^"]+)"', page.text)
    if action is None:
        raise RuntimeError("The login page has no form")
    return browser.post(
        html.unescape(action.group(1)),
        data=fields,
        headers={"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())},
        follow_redirects=False,
    )


def check_login_and_claims(realm: Realm, demo: dict[str, str]) -> Tokens:
    tokens: dict[str, Tokens] = {}
    for alias in TENANTS:
        signed_in = realm.login(demo_email(alias), demo[alias])
        if signed_in is None:
            raise RuntimeError(f"Failed: {alias}'s demo user could not sign in")
        tokens[alias] = signed_in
    check(True, "Demo users sign in through the browser flow, with PKCE (SEC-001)")
    claims = {alias: realm.claims(item.access) for alias, item in tokens.items()}
    check(True, "Tokens verify against the realm's published keys, issuer and audience")
    for alias, item in claims.items():
        check(
            item.get(TENANT_CLAIM) == alias,
            f"{alias}'s token carries one tenant_id of {alias!r} (SEC-005, vendor check)",
        )
    first = next(iter(claims.values()))
    check(
        first["exp"] - first["iat"] <= ACCESS_TOKEN_SECONDS,
        "An access token lasts at most 5 minutes (SEC-001)",
    )
    token = next(iter(tokens.values())).access
    parts = token.split(".")
    payload = parts[1]
    tampered = ".".join(
        [parts[0], payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB"), parts[2]]
    )
    refused = False
    try:
        realm.claims(tampered)
    except jwt.InvalidTokenError:
        refused = True
    check(refused, "A token with a changed tenant or claim fails verification")
    return next(iter(tokens.values()))


def check_user_with_no_tenant(realm: Realm, api: httpx.Client) -> None:
    password = strong_password()
    create_user(api, NO_TENANT_USER, password)
    tokens = realm.login(NO_TENANT_USER, password)
    if tokens is None:
        raise RuntimeError("Failed: the user with no organization could not sign in")
    check(
        TENANT_CLAIM not in realm.claims(tokens.access),
        "A user with no organization gets no tenant_id (vendor check)",
    )


def check_user_in_two_tenants(realm: Realm, api: httpx.Client) -> None:
    """SEC-005 needs one verified tenant, so Keycloak must not pick one of two."""
    password = strong_password()
    user_id = create_user(api, TWO_TENANTS_USER, password)
    organizations = api.get(f"/{REALM}/organizations", params={"first": 0, "max": 100})
    organizations.raise_for_status()
    for organization in organizations.json():
        api.post(
            f"/{REALM}/organizations/{organization['id']}/members",
            content=f'"{user_id}"',
            headers={"Content-Type": "application/json"},
        ).raise_for_status()
    tokens = realm.login(TWO_TENANTS_USER, password)
    check(
        tokens is None or TENANT_CLAIM not in realm.claims(tokens.access),
        "A user in two organizations gets no tenant_id, rather than a guess (vendor check)",
    )


def check_refresh_rotation(realm: Realm, tokens: Tokens) -> None:
    first = realm.refresh(tokens.refresh)
    first.raise_for_status()
    rotated = first.json()["refresh_token"]
    check(rotated != tokens.refresh, "Refreshing issues a new refresh token (SEC-001)")
    check(
        realm.refresh(tokens.refresh).status_code == 400,
        "Using an old refresh token again is refused (SEC-001)",
    )
    check(
        realm.refresh(rotated).status_code == 400,
        "Reusing one also ends that session, in case it was stolen",
    )


def check_session_revocation(realm: Realm, api: httpx.Client, demo: dict[str, str]) -> None:
    alias = next(iter(TENANTS))
    tokens = realm.login(demo_email(alias), demo[alias])
    if tokens is None:
        raise RuntimeError("Failed: the demo user could not sign in")
    user_id = user_id_for(api, demo_email(alias))
    api.post(f"/{REALM}/users/{user_id}/logout").raise_for_status()
    check(
        realm.refresh(tokens.refresh).status_code == 400,
        "Ending a user's sessions makes their refresh token stop working (SEC-004)",
    )


def check_password_policy(api: httpx.Client) -> None:
    user_id = create_user(api, POLICY_CHECK_USER, strong_password())
    for password in ("short1!A", "alllowercase-only-1!", "NoDigitsHere!!!!", "NoSymbols12345678"):
        response = api.put(
            f"/{REALM}/users/{user_id}/reset-password",
            json={"type": "password", "value": password, "temporary": False},
        )
        if response.status_code != 400:
            raise RuntimeError(f"Failed: a weak password was accepted: {response.status_code}")
    check(True, "Weak passwords are refused (SEC-004)")


def check_lockout(realm: Realm, api: httpx.Client) -> None:
    password = strong_password()
    create_user(api, LOCKOUT_USER, password)
    for _ in range(FAILED_LOGINS_BEFORE_LOCKOUT):
        wrong = realm.login(LOCKOUT_USER, strong_password())
        if wrong is not None:
            raise RuntimeError("Failed: a wrong password signed in")
    check(True, "Wrong passwords are refused")
    check(
        realm.login(LOCKOUT_USER, password) is None,
        "After 5 failures, even the right password is refused (SEC-004)",
    )


def check_client_rules(realm: Realm, demo: dict[str, str]) -> None:
    alias = next(iter(TENANTS))
    direct = httpx.post(
        realm.token_url,
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "username": demo_email(alias),
            "password": demo[alias],
        },
        timeout=30,
    )
    check(direct.status_code in {400, 401}, "The browser client cannot send a password directly")
    plain = httpx.get(
        realm.auth_url,
        params={
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": REDIRECT_URI,
        },
        follow_redirects=False,
        timeout=30,
    )
    location = plain.headers.get("Location", "")
    check(
        plain.status_code in {302, 303} and "error=" in location,
        "A login without PKCE is refused",
    )
    elsewhere = httpx.get(
        realm.auth_url,
        params={
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": "https://attacker.example/callback",
            "code_challenge": pkce_pair()[1],
            "code_challenge_method": "S256",
        },
        timeout=30,
    )
    check(elsewhere.status_code == 400, "A login cannot send its code to another site")
    verifier_mismatch(realm, demo, alias)


def verifier_mismatch(realm: Realm, demo: dict[str, str], alias: str) -> None:
    _, challenge = pkce_pair()
    code = realm.authorization_code(demo_email(alias), demo[alias], challenge)
    response = httpx.post(
        realm.token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": pkce_pair()[0],
        },
        timeout=30,
    )
    check(response.status_code == 400, "A stolen code is useless without its PKCE verifier")


def check_database() -> None:
    def scalar(sql: str) -> str:
        return kubectl(
            "exec", POSTGRES_POD, f"--namespace={DATA}", "--", "psql", "--no-psqlrc",
            "--tuples-only", "--no-align", f"--username={POSTGRES_ADMIN}",
            "--dbname=windops_keycloak", f"--command={sql}",
        ).strip()  # fmt: skip

    count = scalar(
        "SELECT count(*) FROM pg_tables "
        "WHERE tablename = 'realm' AND tableowner = 'windops_keycloak'"
    )
    check(count == "1", "Keycloak's tables are in its own database, under its own login")
    allowed = scalar(
        "SELECT has_database_privilege('windops_alarm', 'windops_keycloak', 'CONNECT')"
    )
    check(allowed == "f", "Another service's login cannot reach Keycloak's database (ARC-002)")


def check_restart(base_tokens: Tokens, demo: dict[str, str]) -> None:
    """Logins and issued tokens must keep working, because the signing keys are in PostgreSQL."""
    pod = kubectl(
        "get", "pods", f"--namespace={NAMESPACE}", f"--selector={POD_SELECTOR}",
        "--output=jsonpath={.items[0].metadata.name}",
    )  # fmt: skip
    kubectl("delete", "pod", pod, f"--namespace={NAMESPACE}")
    kubectl("rollout", "status", DEPLOYMENT, f"--namespace={NAMESPACE}", "--timeout=300s")
    with port_forward(SERVICE, NAMESPACE, 8080) as port:
        # A token's issuer holds the address it was issued through, so sign in again here.
        realm = Realm(f"http://127.0.0.1:{port}")
        alias = next(iter(TENANTS))
        tokens = realm.login(demo_email(alias), demo[alias])
        if tokens is None:
            raise RuntimeError("Failed: login does not work after a restart")
        check(
            realm.claims(tokens.access).get(TENANT_CLAIM) == alias,
            "Realm, tenants and users survive a Keycloak restart",
        )
        keys = httpx.get(f"{realm.issuer}/protocol/openid-connect/certs", timeout=30).json()
        kid = jwt.get_unverified_header(base_tokens.access)["kid"]
        check(
            any(item["kid"] == kid for item in keys["keys"]),
            "Its signing key survives too, so tokens already issued stay valid",
        )


def check_metrics() -> None:
    with port_forward("service/windops-prometheus", "windops-observability", 9090) as port:
        for _ in range(30):
            result = httpx.get(
                f"http://127.0.0.1:{port}/api/v1/query",
                params={"query": 'up{workload="windops-keycloak"}'},
                timeout=15,
            ).json()["data"]["result"]
            if result and result[0]["value"][1] == "1":
                check(True, "Prometheus collects Keycloak's metrics")
                return
            time.sleep(5)
    raise RuntimeError("Failed: Prometheus does not collect Keycloak's metrics")


def user_id_for(api: httpx.Client, email: str) -> str:
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    return str(found.json()[0]["id"])


def create_user(api: httpx.Client, email: str, password: str) -> str:
    delete_user(api, email)
    created = api.post(
        f"/{REALM}/users",
        json={
            "username": email,
            "email": email,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Check",
            "lastName": "User",
            "requiredActions": [],
        },
    )
    created.raise_for_status()
    user_id = str(created.headers["Location"].rsplit("/", 1)[-1])
    api.put(
        f"/{REALM}/users/{user_id}/reset-password",
        json={"type": "password", "value": password, "temporary": False},
    ).raise_for_status()
    return user_id


def delete_user(api: httpx.Client, email: str) -> None:
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    for item in found.json():
        api.delete(f"/{REALM}/users/{item['id']}").raise_for_status()


def run(base: str, admin_password: str, demo: dict[str, str]) -> Tokens:
    realm = Realm(base)
    with admin_client(base, admin_password) as api:
        try:
            tokens = check_login_and_claims(realm, demo)
            check_user_with_no_tenant(realm, api)
            check_user_in_two_tenants(realm, api)
            check_refresh_rotation(realm, tokens)
            check_session_revocation(realm, api, demo)
            check_password_policy(api)
            check_lockout(realm, api)
            check_client_rules(realm, demo)
            return tokens
        finally:
            for email in TEMPORARY_USERS:
                delete_user(api, email)


def main() -> int:
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    demo = read_demo_passwords()
    try:
        kubectl("rollout", "status", DEPLOYMENT, f"--namespace={NAMESPACE}", "--timeout=300s")
        with port_forward(SERVICE, NAMESPACE, 8080) as port:
            tokens = run(f"http://127.0.0.1:{port}", admin_password, demo)
        check_database()
        check_restart(tokens, demo)
        check_metrics()
    except httpx.HTTPStatusError as error:
        # The status and address only, because a request can hold a password.
        print(f"Refused: {error.response.status_code} {error.request.url.path}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, httpx.HTTPError, jwt.PyJWTError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Identity check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
