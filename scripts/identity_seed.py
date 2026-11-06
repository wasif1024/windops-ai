#!/usr/bin/env python3
"""Create or update the Keycloak realm, tenants, client and demo users. Safe to repeat.

make identity-seed
"""

import base64
import contextlib
import json
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import settings
from gateway_routes import port_forward
from provision_postgres import read_passwords
from tls_client import trust_cluster_ca
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD, MFA_LEVEL_SECONDS

CONTEXT = settings.KUBE_CONTEXT
NAMESPACE = "windops-system"
DEPLOYMENT = "deployment/windops-keycloak"
SERVICE = "service/windops-keycloak"
POD_SELECTOR = "app.kubernetes.io/name=windops-keycloak"
DEMO_USERS_RESOURCE = "windops-identity-demo-users"
ADMIN_USER = "windops_admin"
REALM = settings.REALM
CLIENT_ID = settings.WEB_CLIENT_ID
API_AUDIENCE = settings.API_AUDIENCE
TENANT_CLAIM = "tenant_id"
ORGANIZATION_SCOPE = "organization"
ADMIN_CLIENT_ID = "windops-admin"
LOGIN_FLOW = "windops-browser"
# How long a password counts, in seconds. windops-auth holds the mfa level's limit (SEC-003).
PASSWORD_LEVEL_SECONDS = 36000
# Everyone who administers must set these up at their next login (SEC-002).
ADMIN_REQUIRED_ACTIONS = ["CONFIGURE_TOTP", "CONFIGURE_RECOVERY_AUTHN_CODES"]
LOCAL = Path(__file__).resolve().parent.parent / "infrastructure" / "minikube"
ADMIN_SECRET_FILE = LOCAL / "secrets.local.keycloak.env"
# Synthetic tenants (SRS 1). The alias becomes the tenant ID in every token.
TENANTS = {"north-sea": "North Sea Wind", "atlas-wind": "Atlas Wind"}
# Platform staff belong to a tenant of their own, so their tokens still carry a tenant (SEC-005).
PLATFORM_TENANT = "windops-platform"
PLATFORM_ADMIN_EMAIL = f"platform-admin@{PLATFORM_TENANT}.example"
PLATFORM_ADMIN_KEY = "platform-admin"
# The frontend behind the gateway, and the other origins in the settings, such as Vite's.
REDIRECT_URIS = [f"{settings.PUBLIC_URL}/*", *(f"{origin}/*" for origin in settings.origins())]


def realm_settings() -> dict[str, Any]:
    """Return the realm's security settings (SEC-001, SEC-004, SEC-005)."""
    return {
        "realm": REALM,
        "enabled": True,
        "organizationsEnabled": True,
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "loginWithEmailAllowed": True,
        # SEC-001: short-lived access tokens, and refresh tokens that each work only once.
        "accessTokenLifespan": 300,
        "revokeRefreshToken": True,
        "refreshTokenMaxReuse": 0,
        "ssoSessionIdleTimeout": 1800,
        "ssoSessionMaxLifespan": 36000,
        # SEC-004.
        "passwordPolicy": (
            "length(12) and upperCase(1) and lowerCase(1) and digits(1) and specialChars(1) "
            "and notUsername(undefined) and passwordHistory(5)"
        ),
        "bruteForceProtected": True,
        "permanentLockout": False,
        "failureFactor": 5,
        "waitIncrementSeconds": 60,
        "maxFailureWaitSeconds": 900,
        "maxDeltaTimeSeconds": 43200,
        # SEC-002: 6-digit codes that change every 30 seconds, each usable once.
        "otpPolicyType": "totp",
        "otpPolicyAlgorithm": "HmacSHA256",
        "otpPolicyDigits": 6,
        "otpPolicyPeriod": 30,
        "otpPolicyLookAheadWindow": 1,
        "otpPolicyCodeReusable": False,
        # SEC-003: names the levels that step-up asks for, and tokens report.
        "attributes": {
            "acr.loa.map": json.dumps({LEVEL_PASSWORD: 1, LEVEL_MFA: 2}),
        },
    }


def tenant_mapper() -> dict[str, Any]:
    """Put the user's organization in each token, where only Keycloak can set it (SEC-005)."""
    return {
        "name": TENANT_CLAIM,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-organization-membership-mapper",
        "config": {
            "claim.name": TENANT_CLAIM,
            "jsonType.label": "String",
            "multivalued": "false",
            "access.token.claim": "true",
            "id.token.claim": "true",
            "introspection.token.claim": "true",
        },
    }


def audience_mapper() -> dict[str, Any]:
    """Name who a token is for, so the gateway can refuse one meant for anything else (M3.7)."""
    return {
        "name": API_AUDIENCE,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.custom.audience": API_AUDIENCE,
            "access.token.claim": "true",
            "id.token.claim": "false",
        },
    }


def client_settings() -> dict[str, Any]:
    """Return the browser client: code flow with PKCE, and no way to send a password directly."""
    return {
        "clientId": CLIENT_ID,
        "enabled": True,
        "publicClient": True,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "fullScopeAllowed": False,
        "redirectUris": REDIRECT_URIS,
        "webOrigins": ["+"],
        "attributes": {
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": "##".join(REDIRECT_URIS),
        },
        "protocolMappers": [tenant_mapper(), audience_mapper()],
    }


def admin_client_settings() -> dict[str, Any]:
    """Return the administrators' client, which never accepts a sign-in without MFA (SEC-002)."""
    settings = client_settings()
    settings["clientId"] = ADMIN_CLIENT_ID
    settings["attributes"] = {**settings["attributes"], "minimum.acr.value": LEVEL_MFA}
    return settings


IDENTITY_CLIENT_ID = "windops-identity-service"
IDENTITY_CLIENT_RESOURCE = "windops-identity-keycloak"
# Keycloak lets nothing less than manage-realm read or change an organization's members. The
# service limits itself to a fixed list of calls, in its keycloak.py.
IDENTITY_ROLES = ("view-users", "query-users", "manage-users", "manage-realm")
KEYCLOAK_URL = settings.KEYCLOAK_INTERNAL_URL
# The one address browsers, and so token issuers, use, from the settings. Locally, the checks
# forward this exact port to the gateway.
PUBLIC_PORT = settings.PUBLIC_PORT
PUBLIC_URL = f"{settings.PUBLIC_URL}/auth"
ADMIN_PORT = 8443
GATEWAY = "service/windops-gateway"


def identity_client_settings() -> dict[str, Any]:
    """A machine client with no browser login, only its own service account."""
    return {
        "clientId": IDENTITY_CLIENT_ID,
        "enabled": True,
        "publicClient": False,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": True,
        "fullScopeAllowed": False,
        "protocolMappers": [],
    }


# Token exchange (SEC-008). windops-api is in every user token's audience, so Keycloak lets it
# exchange one. Each allowed target gets a client scope, and nothing else can be asked for.
EXCHANGE_CLIENT_ID = API_AUDIENCE
EXCHANGE_LIFETIME_SECONDS = 120
EXCHANGE_TARGETS = ("windops-agent-runtime",)


def exchange_client_settings() -> dict[str, Any]:
    """The client the Identity Service exchanges tokens as. It cannot sign anyone in."""
    return {
        "clientId": EXCHANGE_CLIENT_ID,
        "enabled": True,
        "publicClient": False,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "fullScopeAllowed": False,
        "attributes": {
            "standard.token.exchange.enabled": "true",
            "access.token.lifespan": str(EXCHANGE_LIFETIME_SECONDS),
        },
        # Without it, an exchanged token would carry no tenant, which every service refuses.
        "protocolMappers": [tenant_mapper()],
    }


def target_client_settings(client_id: str) -> dict[str, Any]:
    """A service that only receives exchanged tokens, and never signs anyone in."""
    return {
        "clientId": client_id,
        "enabled": True,
        "bearerOnly": True,
        "publicClient": False,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "protocolMappers": [],
    }


def target_scope_name(client_id: str) -> str:
    return f"{client_id}-audience"


def ensure_target_scope(api: httpx.Client, exchange_client: str, target: str) -> None:
    """A scope that names one target in the audience, offered only to the exchange client."""
    name = target_scope_name(target)
    scopes = api.get(f"/{REALM}/client-scopes")
    scopes.raise_for_status()
    found = [item["id"] for item in scopes.json() if item["name"] == name]
    if found:
        scope_id = found[0]
    else:
        created = api.post(
            f"/{REALM}/client-scopes",
            json={
                "name": name,
                "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "true"},
                "protocolMappers": [
                    {
                        "name": "audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": target,
                            "access.token.claim": "true",
                        },
                    }
                ],
            },
        )
        created.raise_for_status()
        scope_id = created.headers["Location"].rsplit("/", 1)[-1]
    api.put(
        f"/{REALM}/clients/{exchange_client}/optional-client-scopes/{scope_id}"
    ).raise_for_status()


def ensure_exchange_client(api: httpx.Client) -> str:
    """Create the exchange client and its targets, and return the exchange client's secret."""
    ensure_client(api, exchange_client_settings())
    ensure_organization_scope(api, EXCHANGE_CLIENT_ID)
    found = api.get(f"/{REALM}/clients", params={"clientId": EXCHANGE_CLIENT_ID})
    found.raise_for_status()
    exchange_client = found.json()[0]["id"]
    for target in EXCHANGE_TARGETS:
        ensure_client(api, target_client_settings(target))
        ensure_target_scope(api, exchange_client, target)
    secret = api.get(f"/{REALM}/clients/{exchange_client}/client-secret")
    secret.raise_for_status()
    return str(secret.json()["value"])


def ensure_identity_client(api: httpx.Client) -> str:
    """Create the Identity Service's client and roles, and return its secret."""
    ensure_client(api, identity_client_settings())
    client = api.get(f"/{REALM}/clients", params={"clientId": IDENTITY_CLIENT_ID})
    client.raise_for_status()
    client_id = client.json()[0]["id"]
    account = api.get(f"/{REALM}/clients/{client_id}/service-account-user")
    account.raise_for_status()
    management = api.get(f"/{REALM}/clients", params={"clientId": "realm-management"})
    management.raise_for_status()
    management_id = management.json()[0]["id"]
    roles = api.get(f"/{REALM}/clients/{management_id}/roles")
    roles.raise_for_status()
    wanted = [role for role in roles.json() if role["name"] in IDENTITY_ROLES]
    if len(wanted) != len(IDENTITY_ROLES):
        raise RuntimeError("Keycloak is missing a realm-management role")
    api.post(
        f"/{REALM}/users/{account.json()['id']}/role-mappings/clients/{management_id}",
        json=wanted,
    ).raise_for_status()
    # The client does not carry every role into its tokens, so name the ones it may carry.
    api.post(
        f"/{REALM}/clients/{client_id}/scope-mappings/clients/{management_id}", json=wanted
    ).raise_for_status()
    secret = api.get(f"/{REALM}/clients/{client_id}/client-secret")
    secret.raise_for_status()
    return str(secret.json()["value"])


def store_identity_client(secret: str, exchange_secret: str) -> None:
    """Give the secret only to the Identity Service's namespace, sent over stdin."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": IDENTITY_CLIENT_RESOURCE, "namespace": NAMESPACE},
        "type": "Opaque",
        "stringData": {
            "url": KEYCLOAK_URL,
            "client-id": IDENTITY_CLIENT_ID,
            "client-secret": secret,
            "exchange-client-id": EXCHANGE_CLIENT_ID,
            "exchange-client-secret": exchange_secret,
        },
    }
    subprocess.run(
        ["kubectl", "--context", CONTEXT, "apply", "--filename=-"],
        input=json.dumps(manifest), capture_output=True, text=True, check=True,
    )  # fmt: skip


@contextlib.contextmanager
def keycloak_admin() -> Iterator[str]:
    """Yield Keycloak's own address, for its admin API, which the gateway does not route."""
    trust_cluster_ca()
    with port_forward(SERVICE, NAMESPACE, ADMIN_PORT) as port:
        yield f"https://127.0.0.1:{port}/auth"


@contextlib.contextmanager
def keycloak_public(*, through_gateway: bool = True) -> Iterator[str]:
    """Yield Keycloak's public address, as a browser reaches it, through the gateway.

    Its pages and redirects name this exact address, so the local port must be 9443. Checks that
    sign in many times a minute pass `through_gateway=False`, and reach Keycloak itself at the
    same address, because the gateway allows 5 sign-ins a minute from one address (SRS 7.5).
    Keycloak's own certificate is valid for localhost, so nothing else differs.
    """
    trust_cluster_ca()
    target, port = (GATEWAY, 443) if through_gateway else (SERVICE, ADMIN_PORT)
    with port_forward(target, NAMESPACE, port, local_port=PUBLIC_PORT):
        yield PUBLIC_URL


def demo_admin_email(alias: str) -> str:
    return f"admin@{alias}.example"


def demo_admin_key(alias: str) -> str:
    return f"admin-{alias}"


def demo_email(alias: str) -> str:
    return f"operator@{alias}.example"


def strong_password() -> str:
    """Return a password that always meets the policy, whatever the random part holds."""
    return f"Wo1!{secrets.token_urlsafe(24)}"


def admin_client(base: str, password: str) -> httpx.Client:
    response = httpx.post(
        f"{base}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": ADMIN_USER,
            "password": password,
        },
        timeout=30,
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    return httpx.Client(
        base_url=f"{base}/admin/realms",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def ensure_realm(api: httpx.Client) -> None:
    if api.get(f"/{REALM}").status_code == 404:
        api.post("", json=realm_settings()).raise_for_status()
    else:
        api.put(f"/{REALM}", json=realm_settings()).raise_for_status()


def ensure_client(api: httpx.Client, settings: dict[str, Any]) -> None:
    found = api.get(f"/{REALM}/clients", params={"clientId": settings["clientId"]})
    found.raise_for_status()
    existing = found.json()
    if not existing:
        api.post(f"/{REALM}/clients", json=settings).raise_for_status()
        return
    client_id = existing[0]["id"]
    mappers = settings.pop("protocolMappers")
    api.put(f"/{REALM}/clients/{client_id}", json=settings).raise_for_status()
    current = api.get(f"/{REALM}/clients/{client_id}/protocol-mappers/models")
    current.raise_for_status()
    named = {item["name"]: item["id"] for item in current.json()}
    for mapper in mappers:
        if mapper["name"] in named:
            mapper_id = named[mapper["name"]]
            api.put(
                f"/{REALM}/clients/{client_id}/protocol-mappers/models/{mapper_id}",
                json={**mapper, "id": mapper_id},
            ).raise_for_status()
        else:
            api.post(
                f"/{REALM}/clients/{client_id}/protocol-mappers/models", json=mapper
            ).raise_for_status()


def ensure_organization_scope(api: httpx.Client, client: str) -> None:
    """Keycloak adds a tenant only with this scope, so a login must not be able to omit it."""
    clients = api.get(f"/{REALM}/clients", params={"clientId": client})
    clients.raise_for_status()
    client_id = clients.json()[0]["id"]
    scopes = api.get(f"/{REALM}/client-scopes")
    scopes.raise_for_status()
    scope_id = next(item["id"] for item in scopes.json() if item["name"] == ORGANIZATION_SCOPE)
    client_url = f"/{REALM}/clients/{client_id}"
    optional = api.delete(f"{client_url}/optional-client-scopes/{scope_id}")
    if optional.status_code not in {204, 404}:
        optional.raise_for_status()
    api.put(f"{client_url}/default-client-scopes/{scope_id}").raise_for_status()


def add_step(api: httpx.Client, flow: str, provider: str, requirement: str) -> str:
    """Add an authenticator to a flow, set its requirement, and return its execution ID."""
    api.post(
        f"/{REALM}/authentication/flows/{flow}/executions/execution", json={"provider": provider}
    ).raise_for_status()
    return set_requirement(api, flow, provider, requirement)


def add_subflow(api: httpx.Client, parent: str, alias: str, requirement: str) -> None:
    api.post(
        f"/{REALM}/authentication/flows/{parent}/executions/flow",
        json={"alias": alias, "type": "basic-flow", "provider": "registration-page-form"},
    ).raise_for_status()
    set_requirement(api, parent, alias, requirement)


def set_requirement(api: httpx.Client, flow: str, name: str, requirement: str) -> str:
    executions = api.get(f"/{REALM}/authentication/flows/{flow}/executions")
    executions.raise_for_status()
    # The newest match, because a flow can hold the same provider in different sub-flows.
    match = [
        item
        for item in executions.json()
        if name in {item.get("providerId"), item.get("displayName")}
    ][-1]
    match["requirement"] = requirement
    api.put(f"/{REALM}/authentication/flows/{flow}/executions", json=match).raise_for_status()
    return str(match["id"])


def add_level(api: httpx.Client, flow: str, level: int, seconds: int) -> None:
    """Make a sub-flow apply only to this level of authentication."""
    execution = add_step(api, flow, "conditional-level-of-authentication", "REQUIRED")
    api.post(
        f"/{REALM}/authentication/executions/{execution}/config",
        json={
            "alias": f"{flow}-condition",
            "config": {"loa-condition-level": str(level), "loa-max-age": str(seconds)},
        },
    ).raise_for_status()


def ensure_login_flow(api: httpx.Client) -> None:
    """Rebuild the login flow, so the result is the same however often this runs.

    Level 1 asks for a password. Level 2 asks for a code or a recovery code, and only when a client
    asks for that level. Keycloak's Organization step is left out, because it would add a page and
    is only needed to redirect a tenant to its own identity provider, which we do not use.
    """
    api.put(f"/{REALM}", json={"browserFlow": "browser"}).raise_for_status()
    flows = api.get(f"/{REALM}/authentication/flows")
    flows.raise_for_status()
    for flow in flows.json():
        if flow["alias"] == LOGIN_FLOW:
            api.delete(f"/{REALM}/authentication/flows/{flow['id']}").raise_for_status()
    api.post(
        f"/{REALM}/authentication/flows",
        json={
            "alias": LOGIN_FLOW,
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
            "description": "Password, then a second factor when a client asks for that level",
        },
    ).raise_for_status()
    add_step(api, LOGIN_FLOW, "auth-cookie", "ALTERNATIVE")
    add_subflow(api, LOGIN_FLOW, f"{LOGIN_FLOW}-forms", "ALTERNATIVE")
    first = f"{LOGIN_FLOW}-password"
    second = f"{LOGIN_FLOW}-second-factor"
    add_subflow(api, f"{LOGIN_FLOW}-forms", first, "CONDITIONAL")
    add_level(api, first, 1, PASSWORD_LEVEL_SECONDS)
    add_step(api, first, "auth-username-password-form", "REQUIRED")
    add_subflow(api, f"{LOGIN_FLOW}-forms", second, "CONDITIONAL")
    add_level(api, second, 2, MFA_LEVEL_SECONDS)
    add_step(api, second, "auth-otp-form", "ALTERNATIVE")
    add_step(api, second, "auth-recovery-authn-code-form", "ALTERNATIVE")
    api.put(f"/{REALM}", json={"browserFlow": LOGIN_FLOW}).raise_for_status()


def ensure_tenant(api: httpx.Client, alias: str, name: str) -> str:
    # Keycloak's search does not match an alias, so read the list and compare.
    found = api.get(f"/{REALM}/organizations", params={"first": 0, "max": 500})
    found.raise_for_status()
    matches = [item for item in found.json() if item["alias"] == alias]
    if matches:
        return str(matches[0]["id"])
    created = api.post(
        f"/{REALM}/organizations",
        json={
            "name": name,
            "alias": alias,
            "enabled": True,
            "domains": [{"name": f"{alias}.example"}],
        },
    )
    created.raise_for_status()
    return str(created.headers["Location"].rsplit("/", 1)[-1])


def ensure_user(
    api: httpx.Client,
    email: str,
    password: str,
    *,
    new_password: bool,
    required_actions: list[str] | None = None,
) -> str:
    """Create a user. An existing one changes password only if its stored one was lost."""
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    if found.json():
        user_id = str(found.json()[0]["id"])
        if new_password:
            api.put(
                f"/{REALM}/users/{user_id}/reset-password",
                json={"type": "password", "value": password, "temporary": False},
            ).raise_for_status()
        return user_id
    created = api.post(
        f"/{REALM}/users",
        json={
            "username": email,
            "email": email,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Demo",
            "lastName": "Operator",
            "requiredActions": required_actions or [],
        },
    )
    created.raise_for_status()
    user_id = str(created.headers["Location"].rsplit("/", 1)[-1])
    api.put(
        f"/{REALM}/users/{user_id}/reset-password",
        json={"type": "password", "value": password, "temporary": False},
    ).raise_for_status()
    return user_id


def ensure_member(api: httpx.Client, organization_id: str, user_id: str) -> None:
    members = api.get(f"/{REALM}/organizations/{organization_id}/members")
    members.raise_for_status()
    if not any(item["id"] == user_id for item in members.json()):
        api.post(
            f"/{REALM}/organizations/{organization_id}/members",
            content=f'"{user_id}"',
            headers={"Content-Type": "application/json"},
        ).raise_for_status()


def seed(
    base: str, admin_password: str, demo_passwords: dict[str, str], stored: dict[str, str]
) -> None:
    with admin_client(base, admin_password) as api:
        ensure_realm(api)
        ensure_login_flow(api)
        for settings in (client_settings(), admin_client_settings()):
            ensure_client(api, settings)
            ensure_organization_scope(api, settings["clientId"])
        for alias, name in TENANTS.items():
            organization_id = ensure_tenant(api, alias, name)
            user_id = ensure_user(
                api, demo_email(alias), demo_passwords[alias], new_password=alias not in stored
            )
            ensure_member(api, organization_id, user_id)
            key = demo_admin_key(alias)
            # Only when created, so an administrator who has enrolled is not asked again.
            admin_id = ensure_user(
                api,
                demo_admin_email(alias),
                demo_passwords[key],
                new_password=key not in stored,
                required_actions=ADMIN_REQUIRED_ACTIONS,
            )
            ensure_member(api, organization_id, admin_id)
        platform_id = ensure_tenant(api, PLATFORM_TENANT, "WindOps Platform")
        platform_admin = ensure_user(
            api,
            PLATFORM_ADMIN_EMAIL,
            demo_passwords[PLATFORM_ADMIN_KEY],
            new_password=PLATFORM_ADMIN_KEY not in stored,
            required_actions=ADMIN_REQUIRED_ACTIONS,
        )
        ensure_member(api, platform_id, platform_admin)
        store_identity_client(ensure_identity_client(api), ensure_exchange_client(api))


def missing_demo_passwords(existing: dict[str, str]) -> dict[str, str]:
    """Return every demo password, adding any that do not exist yet and keeping the rest."""
    keys = [*TENANTS, *(demo_admin_key(alias) for alias in TENANTS), PLATFORM_ADMIN_KEY]
    return {key: existing.get(key) or strong_password() for key in keys}


def read_demo_passwords() -> dict[str, str]:
    """Read the demo passwords from their Secret, or return nothing if it does not exist yet."""
    result = subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "get", "secret", DEMO_USERS_RESOURCE,
            f"--namespace={NAMESPACE}", "--output=json", "--ignore-not-found",
        ],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    if not result.stdout.strip():
        return {}
    data = json.loads(result.stdout).get("data", {})
    return {key: base64.b64decode(value).decode() for key, value in data.items()}


def store_demo_passwords(passwords: dict[str, str]) -> None:
    """Keep them in the cluster, sent over stdin so no password appears in a process listing."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": DEMO_USERS_RESOURCE, "namespace": NAMESPACE},
        "type": "Opaque",
        "stringData": passwords,
    }
    subprocess.run(
        ["kubectl", "--context", CONTEXT, "apply", "--filename=-"],
        input=json.dumps(manifest), capture_output=True, text=True, check=True,
    )  # fmt: skip


def wait_for_keycloak() -> None:
    subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "rollout", "status", DEPLOYMENT,
            f"--namespace={NAMESPACE}", "--timeout=300s",
        ],
        check=True,
    )  # fmt: skip


def main() -> int:
    if not ADMIN_SECRET_FILE.exists():
        print("No Keycloak password found. Run: make secrets", file=sys.stderr)
        return 1
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    try:
        stored = read_demo_passwords()
        demo = missing_demo_passwords(stored)
        store_demo_passwords(demo)
    except subprocess.CalledProcessError as error:
        print(f"Could not read or store the demo passwords: {error.stderr}", file=sys.stderr)
        return 1
    wait_for_keycloak()
    try:
        with keycloak_admin() as base:
            seed(base, admin_password, demo, stored)
    except httpx.HTTPStatusError as error:
        # The status and address only, because a request body can hold a password.
        print(
            f"Keycloak refused a request: {error.response.status_code} {error.request.url.path}",
            file=sys.stderr,
        )
        return 1
    except (httpx.HTTPError, RuntimeError, OSError) as error:
        print(f"Could not reach Keycloak: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Seeded the {REALM} realm, {len(TENANTS)} tenants and their demo users")
    return 0


if __name__ == "__main__":
    sys.exit(main())
