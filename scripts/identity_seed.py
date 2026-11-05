#!/usr/bin/env python3
"""Create or update the Keycloak realm, tenants, client and demo users. Safe to repeat.

make identity-seed
"""

import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from gateway_routes import port_forward
from provision_postgres import read_passwords

CONTEXT = "windops"
NAMESPACE = "windops-system"
DEPLOYMENT = "deployment/windops-keycloak"
SERVICE = "service/windops-keycloak"
POD_SELECTOR = "app.kubernetes.io/name=windops-keycloak"
ADMIN_USER = "windops_admin"
REALM = "windops"
CLIENT_ID = "windops-web"
API_AUDIENCE = "windops-api"
TENANT_CLAIM = "tenant_id"
ORGANIZATION_SCOPE = "organization"
LOCAL = Path(__file__).resolve().parent.parent / "infrastructure" / "minikube"
ADMIN_SECRET_FILE = LOCAL / "secrets.local.keycloak.env"
DEMO_SECRET_FILE = LOCAL / "secrets.local.identity.env"
# Synthetic tenants (SRS 1). The alias becomes the tenant ID in every token.
TENANTS = {"north-sea": "North Sea Wind", "atlas-wind": "Atlas Wind"}
REDIRECT_URIS = ["http://localhost:5173/*", "http://127.0.0.1:5173/*"]


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
        "attributes": {"pkce.code.challenge.method": "S256"},
        "protocolMappers": [tenant_mapper(), audience_mapper()],
    }


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


def ensure_client(api: httpx.Client) -> None:
    found = api.get(f"/{REALM}/clients", params={"clientId": CLIENT_ID})
    found.raise_for_status()
    existing = found.json()
    settings = client_settings()
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


def ensure_organization_scope(api: httpx.Client) -> None:
    """Keycloak adds a tenant only with this scope, so a login must not be able to omit it."""
    clients = api.get(f"/{REALM}/clients", params={"clientId": CLIENT_ID})
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


def ensure_user(api: httpx.Client, email: str, password: str) -> str:
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    if found.json():
        return str(found.json()[0]["id"])
    created = api.post(
        f"/{REALM}/users",
        json={
            "username": email,
            "email": email,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Demo",
            "lastName": "Operator",
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


def ensure_member(api: httpx.Client, organization_id: str, user_id: str) -> None:
    members = api.get(f"/{REALM}/organizations/{organization_id}/members")
    members.raise_for_status()
    if not any(item["id"] == user_id for item in members.json()):
        api.post(
            f"/{REALM}/organizations/{organization_id}/members",
            content=f'"{user_id}"',
            headers={"Content-Type": "application/json"},
        ).raise_for_status()


def seed(base: str, admin_password: str, demo_passwords: dict[str, str]) -> None:
    with admin_client(base, admin_password) as api:
        ensure_realm(api)
        ensure_client(api)
        ensure_organization_scope(api)
        for alias, name in TENANTS.items():
            organization_id = ensure_tenant(api, alias, name)
            user_id = ensure_user(api, demo_email(alias), demo_passwords[alias])
            ensure_member(api, organization_id, user_id)


def ensure_demo_passwords(path: Path = DEMO_SECRET_FILE) -> dict[str, str]:
    """Create any missing demo password, keeping existing ones, readable only by their owner."""
    path.touch(mode=0o600)
    existing = read_passwords(path)
    with path.open("a") as file:
        for alias in TENANTS:
            if alias not in existing:
                file.write(f"{alias}={strong_password()}\n")
    return read_passwords(path)


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
    demo = ensure_demo_passwords()
    wait_for_keycloak()
    try:
        with port_forward(SERVICE, NAMESPACE, 8080) as port:
            seed(f"http://127.0.0.1:{port}", admin_password, demo)
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
