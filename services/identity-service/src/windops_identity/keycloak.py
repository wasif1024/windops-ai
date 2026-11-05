"""The only Keycloak admin calls this service can make.

Its service account holds manage-realm, because Keycloak lets nothing less read or change an
organization's members, which are our tenants. That role could also change the realm, so every
call goes through `_call`, which refuses anything not in ALLOWED. A test fails if the list grows.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from windops_resilience import BreakerConfig, CircuitBreaker

TIMEOUT_SECONDS = 5.0
# Fetch a new token this long before the old one expires.
TOKEN_MARGIN_SECONDS = 30
# (method, path template). Placeholders are filled in, and escaped, by `_call`.
ALLOWED = frozenset(
    {
        ("GET", "organizations"),
        ("GET", "organizations/{org}/members"),
        ("GET", "organizations/{org}/members/{user}"),
        ("POST", "organizations/{org}/members"),
        ("GET", "users"),
        ("POST", "users"),
        ("GET", "users/{user}"),
        ("PUT", "users/{user}"),
        ("DELETE", "users/{user}"),
        ("PUT", "users/{user}/reset-password"),
        ("GET", "users/{user}/sessions"),
        ("POST", "users/{user}/logout"),
        ("GET", "users/{user}/credentials"),
    }
)


class KeycloakError(Exception):
    """Keycloak refused a call, or could not be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotAllowedError(RuntimeError):
    """The code tried a call outside ALLOWED, which is a bug, never a user's mistake."""


@dataclass(frozen=True)
class KeycloakSettings:
    url: str
    realm: str
    client_id: str
    client_secret: str


def keycloak_breaker() -> CircuitBreaker:
    return CircuitBreaker("keycloak", BreakerConfig(slow_call_seconds=2.0))


class KeycloakAdmin:
    def __init__(
        self,
        settings: KeycloakSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.url, timeout=TIMEOUT_SECONDS, transport=transport
        )
        self._breaker = breaker or keycloak_breaker()
        self._token: str | None = None
        self._token_expires = 0.0
        self._organizations: dict[str, str] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._token_expires:
            return self._token
        response = await self._http.post(
            f"/realms/{self._settings.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
            },
        )
        if response.status_code != 200:
            raise KeycloakError("Keycloak refused the service's login", response.status_code)
        body = response.json()
        self._token = str(body["access_token"])
        self._token_expires = time.monotonic() + int(body["expires_in"]) - TOKEN_MARGIN_SECONDS
        return self._token

    async def _call(
        self,
        method: str,
        template: str,
        *,
        ids: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        json: Any = None,
        content: str | None = None,
    ) -> httpx.Response:
        if (method, template) not in ALLOWED:
            raise NotAllowedError(f"{method} {template} is not an allowed Keycloak call")
        path = template.format(**{key: quote(value, safe="") for key, value in (ids or {}).items()})

        async def send() -> httpx.Response:
            headers = {"Authorization": f"Bearer {await self._access_token()}"}
            if content is not None:
                headers["Content-Type"] = "application/json"
            response = await self._http.request(
                method,
                f"/admin/realms/{self._settings.realm}/{path}",
                params=params,
                json=json,
                content=content,
                headers=headers,
            )
            if response.status_code >= 500:
                # Counted by the breaker as Keycloak failing, not as a refusal.
                response.raise_for_status()
            return response

        try:
            return await self._breaker.call(send)
        except httpx.HTTPError as error:
            raise KeycloakError(f"Keycloak is unavailable: {type(error).__name__}") from error

    @staticmethod
    def _ok(response: httpx.Response, *expected: int) -> httpx.Response:
        if response.status_code not in expected:
            raise KeycloakError("Keycloak refused a call", response.status_code)
        return response

    async def organization_id(self, tenant_id: str) -> str | None:
        """Return the organization whose alias is the tenant ID. Aliases never change."""
        if tenant_id not in self._organizations:
            # Keycloak's search does not match an alias, so read the list and compare.
            listed = self._ok(
                await self._call("GET", "organizations", params={"first": 0, "max": 1000}), 200
            )
            for item in listed.json():
                self._organizations[str(item["alias"])] = str(item["id"])
        return self._organizations.get(tenant_id)

    async def is_member(self, organization: str, user: str) -> bool:
        response = await self._call(
            "GET", "organizations/{org}/members/{user}", ids={"org": organization, "user": user}
        )
        if response.status_code == 404:
            return False
        self._ok(response, 200)
        return True

    async def members(self, organization: str, first: int, maximum: int) -> list[dict[str, Any]]:
        response = self._ok(
            await self._call(
                "GET",
                "organizations/{org}/members",
                ids={"org": organization},
                params={"first": first, "max": maximum},
            ),
            200,
        )
        members: list[dict[str, Any]] = response.json()
        return members

    async def add_member(self, organization: str, user: str) -> None:
        self._ok(
            await self._call(
                "POST",
                "organizations/{org}/members",
                ids={"org": organization},
                content=f'"{user}"',
            ),
            201,
            204,
        )

    async def find_by_email(self, email: str) -> list[dict[str, Any]]:
        response = self._ok(
            await self._call("GET", "users", params={"email": email, "exact": "true"}), 200
        )
        found: list[dict[str, Any]] = response.json()
        return found

    async def create_user(self, representation: dict[str, Any]) -> str:
        response = self._ok(await self._call("POST", "users", json=representation), 201)
        return response.headers["Location"].rsplit("/", 1)[-1]

    async def user(self, user: str) -> dict[str, Any]:
        response = self._ok(await self._call("GET", "users/{user}", ids={"user": user}), 200)
        body: dict[str, Any] = response.json()
        return body

    async def set_enabled(self, user: str, enabled: bool) -> None:
        self._ok(
            await self._call("PUT", "users/{user}", ids={"user": user}, json={"enabled": enabled}),
            204,
        )

    async def delete_user(self, user: str) -> None:
        self._ok(await self._call("DELETE", "users/{user}", ids={"user": user}), 204, 404)

    async def set_temporary_password(self, user: str, password: str) -> None:
        self._ok(
            await self._call(
                "PUT",
                "users/{user}/reset-password",
                ids={"user": user},
                json={"type": "password", "value": password, "temporary": True},
            ),
            204,
        )

    async def sessions(self, user: str) -> list[dict[str, Any]]:
        response = self._ok(
            await self._call("GET", "users/{user}/sessions", ids={"user": user}), 200
        )
        sessions: list[dict[str, Any]] = response.json()
        return sessions

    async def logout(self, user: str) -> None:
        self._ok(await self._call("POST", "users/{user}/logout", ids={"user": user}), 204)

    async def credentials(self, user: str) -> list[dict[str, Any]]:
        response = self._ok(
            await self._call("GET", "users/{user}/credentials", ids={"user": user}), 200
        )
        credentials: list[dict[str, Any]] = response.json()
        return credentials
