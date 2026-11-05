#!/usr/bin/env python3
"""Prove MFA and step-up authentication work on the real local cluster, through real logins.

Run `make identity-seed` first. This takes about 7 minutes, because it waits for an MFA level to
expire, and for time-based codes to change. Its temporary user is removed afterwards.
"""

import base64
import hashlib
import hmac
import html
import re
import struct
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from gateway_routes import port_forward
from identity_seed import (
    ADMIN_CLIENT_ID,
    ADMIN_REQUIRED_ACTIONS,
    ADMIN_SECRET_FILE,
    CLIENT_ID,
    NAMESPACE,
    REALM,
    SERVICE,
    TENANT_CLAIM,
    TENANTS,
    admin_client,
    demo_email,
    read_demo_passwords,
    strong_password,
)
from provision_postgres import read_passwords
from verify_identity import REDIRECT_URI, Realm, check, create_user, delete_user, pkce_pair
from windops_auth import (
    LEVEL_MFA,
    LEVEL_PASSWORD,
    MFA_LEVEL_SECONDS,
    MfaRequiredError,
    StepUpRequiredError,
    require_mfa,
    require_recent_mfa,
)

ADMIN_USER_EMAIL = "mfa-check-admin@windops-check.example"
CODE_PERIOD_SECONDS = 30
TENANT = next(iter(TENANTS))


class Browser:
    """Keeps cookies as a browser would, including Secure ones, which it allows on localhost."""

    def __init__(self, realm: Realm) -> None:
        self.realm = realm
        self.jar: dict[str, str] = {}
        self.page: httpx.Response | None = None
        self.verifier = ""
        self.client_id = ""

    def _absorb(self, response: httpx.Response) -> httpx.Response:
        for header in response.headers.get_list("set-cookie"):
            name, _, value = header.split(";", 1)[0].partition("=")
            if value:
                self.jar[name.strip()] = value
            else:
                self.jar.pop(name.strip(), None)
        return response

    def _headers(self) -> dict[str, str]:
        return {"Cookie": "; ".join(f"{name}={value}" for name, value in self.jar.items())}

    def _follow(self, response: httpx.Response) -> httpx.Response:
        """Follow redirects inside Keycloak, and stop at the one that returns to the client."""
        while response.status_code in {302, 303}:
            target = html.unescape(response.headers["Location"])
            if target.startswith(REDIRECT_URI):
                break
            response = self._absorb(httpx.get(target, headers=self._headers(), timeout=30))
        self.page = response
        return response

    def start(self, client_id: str, **parameters: str) -> httpx.Response:
        self.client_id = client_id
        self.verifier, challenge = pkce_pair()
        response = httpx.get(
            self.realm.auth_url,
            params={
                "client_id": client_id,
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                **parameters,
            },
            headers=self._headers(),
            timeout=30,
        )
        return self._follow(self._absorb(response))

    def submit(self, fields: dict[str, str]) -> httpx.Response:
        if self.page is None:
            raise RuntimeError("No page to submit")
        action = re.search(r'action="([^"]+)"', self.page.text)
        if action is None:
            raise RuntimeError(f"The page has no form: {self.page.status_code}")
        response = httpx.post(
            html.unescape(action.group(1)), data=fields, headers=self._headers(), timeout=30
        )
        return self._follow(self._absorb(response))

    def get_link(self, pattern: str) -> httpx.Response:
        if self.page is None:
            raise RuntimeError("No page")
        link = re.search(pattern, self.page.text)
        if link is None:
            raise RuntimeError("The page has no such link")
        target = html.unescape(link.group(1))
        url = target if target.startswith("http") else self.realm.base + target
        return self._follow(self._absorb(httpx.get(url, headers=self._headers(), timeout=30)))

    def finished(self) -> bool:
        page = self.page
        return (
            page is not None
            and page.status_code in {302, 303}
            and "code=" in page.headers.get("Location", "")
        )

    def tokens(self) -> dict[str, Any]:
        if self.page is None or not self.finished():
            raise RuntimeError("The login did not finish")
        code = parse_qs(urlparse(self.page.headers["Location"]).query)["code"][0]
        response = httpx.post(
            self.realm.token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": self.verifier,
            },
            timeout=30,
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


def sign_in_with_password(browser: Browser, email: str, password: str) -> httpx.Response:
    return browser.submit({"username": email, "password": password, "credentialId": ""})


def has_field(response: httpx.Response, name: str) -> bool:
    return f'name="{name}"' in response.text


def time_code(secret: str, moment: float | None = None) -> str:
    """Compute the current code (RFC 6238), as an authenticator app does."""
    compact = secret.replace(" ", "").upper()
    key = base64.b32decode(compact + "=" * (-len(compact) % 8))
    counter = int((time.time() if moment is None else moment) // CODE_PERIOD_SECONDS)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha256).digest()
    start = digest[-1] & 15
    value = struct.unpack(">I", digest[start : start + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def wrong_code(secret: str) -> str:
    """Return a code that is valid for no window Keycloak would accept."""
    now = time.time()
    valid = {time_code(secret, now + step * CODE_PERIOD_SECONDS) for step in (-2, -1, 0, 1, 2)}
    return next(item for item in ("000000", "111111", "222222", "333333") if item not in valid)


def wait_for_new_code(secret: str, used: set[str]) -> str:
    """Wait until the current code has not been used yet, since each works only once."""
    while (code := time_code(secret)) in used:
        time.sleep(2)
    used.add(code)
    return code


class Enrolled:
    """What an administrator's authenticator app and printed recovery codes would hold."""

    def __init__(self, secret: str, recovery_codes: list[str]) -> None:
        self.secret = secret
        self.recovery_codes = recovery_codes
        self.used: set[str] = set()

    def code(self) -> str:
        return wait_for_new_code(self.secret, self.used)


def enroll(browser: Browser, email: str, password: str) -> Enrolled:
    browser.start(CLIENT_ID)
    page = sign_in_with_password(browser, email, password)
    check(
        has_field(page, "totp") and not browser.finished(),
        "An administrator's password alone does not finish the first login (SEC-002)",
    )
    page = browser.get_link(r'href="([^"]*mode=manual[^"]*)"')
    found = re.search(r'id="kc-totp-secret-key"[^>]*>\s*([^<]+)<', page.text)
    if found is None:
        raise RuntimeError("The setup page shows no key")
    secret = found.group(1).strip()
    fields = hidden_fields(page)
    refused = browser.submit({**fields, "totp": wrong_code(secret), "userLabel": "check"})
    check(has_field(refused, "totp"), "Setting up with a wrong code is refused")
    fields = hidden_fields(refused)
    used = time_code(secret)
    page = browser.submit({**fields, "totp": used, "userLabel": "check"})
    check(has_field(page, "generatedRecoveryAuthnCodes"), "Recovery codes are required next")
    codes = re.findall(r"<li>\s*([A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4})\s*</li>", page.text)
    if not codes:
        raise RuntimeError("The page shows no recovery codes")
    check(len(codes) >= 12, f"{len(codes)} one-time recovery codes are issued (SEC-002)")
    page = browser.submit({**hidden_fields(page), "kcRecoveryCodesConfirmationCheck": "on"})
    check(browser.finished(), "Setup then finishes the login")
    enrolled = Enrolled(secret, codes)
    enrolled.used.add(used)
    return enrolled


def hidden_fields(page: httpx.Response) -> dict[str, str]:
    """Return a form's preset values, whichever order their attributes appear in."""
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", page.text):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and value:
            fields[name.group(1)] = html.unescape(value.group(1))
    return fields


def check_admin_client_refuses_no_second_factor(realm: Realm, demo: dict[str, str]) -> None:
    browser = Browser(realm)
    browser.start(ADMIN_CLIENT_ID)
    page = sign_in_with_password(browser, demo_email(TENANT), demo[TENANT])
    check(
        not browser.finished() and page.status_code >= 400,
        "A user with no second factor cannot use the administrators' client (SEC-002)",
    )


def check_admin_login(realm: Realm, email: str, password: str, enrolled: Enrolled) -> None:
    browser = Browser(realm)
    browser.start(ADMIN_CLIENT_ID)
    page = sign_in_with_password(browser, email, password)
    check(
        has_field(page, "otp") and not browser.finished(),
        "The administrators' client asks for a code after the password (SEC-002)",
    )
    page = browser.submit({"otp": wrong_code(enrolled.secret)})
    check(has_field(page, "otp") and not browser.finished(), "A wrong code is refused")
    browser.submit({"otp": enrolled.code()})
    claims = realm.claims(browser.tokens()["access_token"])
    check(claims.get("acr") == LEVEL_MFA, "A token from that login reports the mfa level")
    check(claims.get(TENANT_CLAIM) == TENANT, "It still carries the verified tenant (SEC-005)")


def check_code_cannot_be_replayed(
    realm: Realm, email: str, password: str, enrolled: Enrolled
) -> None:
    first = Browser(realm)
    first.start(ADMIN_CLIENT_ID)
    sign_in_with_password(first, email, password)
    code = enrolled.code()
    first.submit({"otp": code})
    check(first.finished(), "A new code signs in")
    second = Browser(realm)
    second.start(ADMIN_CLIENT_ID)
    sign_in_with_password(second, email, password)
    second.submit({"otp": code})
    check(not second.finished(), "The same code cannot be used twice")


def check_recovery_code(realm: Realm, email: str, password: str, enrolled: Enrolled) -> None:
    code = enrolled.recovery_codes[0]
    browser = Browser(realm)
    browser.start(ADMIN_CLIENT_ID)
    page = sign_in_with_password(browser, email, password)
    # The "Try another way" link, then the choice of a recovery code.
    page = browser.submit({"tryAnotherWay": "on"})
    executions = re.findall(r'name="authenticationExecution"\s+value="([^"]+)"', page.text) or (
        re.findall(r'value="([^"]+)"\s+name="authenticationExecution"', page.text)
    )
    if not executions:
        raise RuntimeError("The page offers no other way to sign in")
    # Recovery codes are the option that is not the current code form.
    page = browser.submit({"authenticationExecution": executions[-1]})
    if not has_field(page, "recoveryCodeInput"):
        page = browser.submit({"authenticationExecution": executions[0]})
    check(has_field(page, "recoveryCodeInput"), "A user can choose a recovery code instead")
    number = re.search(r"(?i)recovery[^<]*#\s*(\d+)", page.text)
    position = int(number.group(1)) - 1 if number else 0
    code = enrolled.recovery_codes[position]
    browser.submit({"recoveryCodeInput": code})
    claims = realm.claims(browser.tokens()["access_token"])
    check(claims.get("acr") == LEVEL_MFA, "A recovery code counts as the mfa level (SEC-002)")


def check_step_up(realm: Realm, email: str, password: str, enrolled: Enrolled) -> dict[str, Any]:
    browser = Browser(realm)
    browser.start(CLIENT_ID)
    sign_in_with_password(browser, email, password)
    tokens = browser.tokens()
    first = realm.claims(tokens["access_token"])
    check(first.get("acr") == LEVEL_PASSWORD, "A normal login reports the password level")
    page = browser.start(CLIENT_ID, acr_values=LEVEL_MFA)
    check(
        has_field(page, "otp") and not has_field(page, "password"),
        "Asking for the mfa level then asks only for a code, in the same session (SEC-003)",
    )
    browser.submit({"otp": enrolled.code()})
    stepped = browser.tokens()
    claims = realm.claims(stepped["access_token"])
    check(claims.get("acr") == LEVEL_MFA, "Step-up raises the token to the mfa level (SEC-003)")
    check(
        claims["auth_time"] > first["auth_time"],
        "Step-up moves auth_time, so a service can tell how recent it was",
    )
    require_recent_mfa(claims)
    check(True, "windops-auth accepts a fresh step-up")
    refused = False
    try:
        require_mfa(first)
    except MfaRequiredError:
        refused = True
    check(refused, "windops-auth refuses a password-level token for administrative access")
    return {"browser": browser, "tokens": stepped, "claims": claims}


def check_step_up_without_second_factor(realm: Realm, demo: dict[str, str]) -> None:
    browser = Browser(realm)
    browser.start(CLIENT_ID)
    sign_in_with_password(browser, demo_email(TENANT), demo[TENANT])
    browser.tokens()
    browser.start(CLIENT_ID, acr_values=LEVEL_MFA)
    if browser.finished():
        level = realm.claims(browser.tokens()["access_token"]).get("acr")
        check(level != LEVEL_MFA, f"With no second factor, a token stays at {level!r}")
    else:
        check(True, "With no second factor, step-up is refused")


def check_expiry(
    realm: Realm, stepped: dict[str, Any], enrolled: Enrolled, email: str, password: str
) -> None:
    """Report what happens once the mfa level's time limit has passed."""
    wait = MFA_LEVEL_SECONDS + 10
    print(f"Waiting {wait} seconds for the mfa level to expire", flush=True)
    time.sleep(wait)
    refreshed = realm.refresh(stepped["tokens"]["refresh_token"])
    refreshed.raise_for_status()
    claims = realm.claims(refreshed.json()["access_token"])
    # Keycloak still reports mfa here, so a check of acr alone would accept this (SEC-003).
    refused = False
    try:
        require_recent_mfa(claims)
    except StepUpRequiredError:
        refused = True
    check(refused, f"windops-auth refuses a refreshed token past the limit (acr={claims['acr']!r})")
    browser: Browser = stepped["browser"]
    page = browser.start(CLIENT_ID, acr_values=LEVEL_MFA)
    check(has_field(page, "otp"), "After the time limit, asking for mfa asks for a code again")
    browser.submit({"otp": enrolled.code()})
    restored = realm.claims(browser.tokens()["access_token"])
    require_recent_mfa(restored)
    check(True, "A new code restores it, and windops-auth accepts it")
    check_password_cannot_refresh_a_stale_step_up(realm, email, password)


def check_password_cannot_refresh_a_stale_step_up(realm: Realm, email: str, password: str) -> None:
    """The obvious attack: sign in again with only a password, to make auth_time recent."""
    browser = Browser(realm)
    browser.start(CLIENT_ID)
    sign_in_with_password(browser, email, password)
    claims = realm.claims(browser.tokens()["access_token"])
    refused = False
    try:
        require_recent_mfa(claims)
    except MfaRequiredError:
        refused = True
    check(refused, f"A new password-only login is refused too (acr={claims.get('acr')!r})")
    browser.start(CLIENT_ID, prompt="login")
    sign_in_with_password(browser, email, password)
    claims = realm.claims(browser.tokens()["access_token"])
    refused = False
    try:
        require_recent_mfa(claims)
    except MfaRequiredError:
        refused = True
    check(refused, f"So is re-entering a password in that session (acr={claims.get('acr')!r})")


def run(base: str, admin_password: str, demo: dict[str, str]) -> None:
    realm = Realm(base)
    with admin_client(base, admin_password) as api:
        password = strong_password()
        user_id = create_user(api, ADMIN_USER_EMAIL, password)
        try:
            user = api.get(f"/{REALM}/users/{user_id}").json()
            user["requiredActions"] = ADMIN_REQUIRED_ACTIONS
            api.put(f"/{REALM}/users/{user_id}", json=user).raise_for_status()
            organizations = api.get(f"/{REALM}/organizations", params={"first": 0, "max": 100})
            organizations.raise_for_status()
            organization = next(i for i in organizations.json() if i["alias"] == TENANT)
            api.post(
                f"/{REALM}/organizations/{organization['id']}/members",
                content=f'"{user_id}"',
                headers={"Content-Type": "application/json"},
            ).raise_for_status()
            check_admin_client_refuses_no_second_factor(realm, demo)
            enrolled = enroll(Browser(realm), ADMIN_USER_EMAIL, password)
            check_admin_login(realm, ADMIN_USER_EMAIL, password, enrolled)
            check_code_cannot_be_replayed(realm, ADMIN_USER_EMAIL, password, enrolled)
            check_recovery_code(realm, ADMIN_USER_EMAIL, password, enrolled)
            stepped = check_step_up(realm, ADMIN_USER_EMAIL, password, enrolled)
            check_step_up_without_second_factor(realm, demo)
            check_expiry(realm, stepped, enrolled, ADMIN_USER_EMAIL, password)
        finally:
            # A new sign-in, because the admin token above expires during the long wait.
            with admin_client(base, admin_password) as cleanup:
                delete_user(cleanup, ADMIN_USER_EMAIL)


def main() -> int:
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    demo = read_demo_passwords()
    try:
        with port_forward(SERVICE, NAMESPACE, 8080) as port:
            run(f"http://127.0.0.1:{port}", admin_password, demo)
    except httpx.HTTPStatusError as error:
        # The status and address only, because a request can hold a password.
        print(f"Refused: {error.response.status_code} {error.request.url.path}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, httpx.HTTPError, jwt.PyJWTError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("MFA check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
