"""Verify a browser's Keycloak access token, as every request must (SEC-001, SEC-005)."""

import asyncio
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 10
# Keycloak's keys change rarely. An unknown key ID fetches them again, at most this often.
REFRESH_SECONDS = 60


# What a browser may be told about a refused token. Only these, never an exception's own text.
REASONS = {
    "missing": ("missing_token", "A bearer token is required"),
    "invalid": ("invalid_token", "The token is not valid"),
    "no_tenant": (
        "no_tenant",
        "The token carries no tenant, so access to it is suspended or unclear",
    ),
    "keys_unavailable": ("keys_unavailable", "Sign-in cannot be checked right now"),
}


class TokenError(Exception):
    """The token is missing, invalid, expired, or carries no single tenant."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason if reason in REASONS else "invalid"


@dataclass(frozen=True)
class Caller:
    """What a verified token says. Everything downstream trusts only this."""

    user_id: str
    tenant_id: str
    email: str
    # The client the user signed in through, such as "windops-admin".
    client: str
    auth_level: str | None
    auth_time: int | None


@dataclass(frozen=True)
class TokenSettings:
    issuer: str
    audience: str
    jwks_url: str
    ca_file: str | None = None


def bearer(header: str | None) -> str:
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise TokenError("missing")
    return token.strip()


class TokenVerifier:
    def __init__(
        self,
        settings: TokenSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._settings = settings
        verify: ssl.SSLContext | bool = True
        if settings.ca_file:
            verify = ssl.create_default_context(cafile=settings.ca_file)
            verify.minimum_version = ssl.TLSVersion.TLSv1_2
        self._http = httpx.AsyncClient(timeout=5.0, transport=transport, verify=verify)
        self._keys: dict[str, Any] = {}
        # None until the first fetch. A monotonic clock counts from the machine's boot, so on a
        # freshly started machine it can be under REFRESH_SECONDS, which must not block that fetch.
        self._fetched: float | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    def _may_fetch(self) -> bool:
        return self._fetched is None or self._clock() - self._fetched > REFRESH_SECONDS

    async def _key(self, key_id: str) -> Any:
        if key_id not in self._keys and self._may_fetch():
            async with self._lock:
                if key_id not in self._keys:
                    try:
                        response = await self._http.get(self._settings.jwks_url)
                        response.raise_for_status()
                    except httpx.HTTPError as error:
                        raise TokenError("keys_unavailable") from error
                    self._fetched = self._clock()
                    self._keys = {
                        item["kid"]: jwt.PyJWK(item).key
                        for item in response.json()["keys"]
                        if item.get("use", "sig") == "sig" and item.get("alg") in ALGORITHMS
                    }
        if key_id not in self._keys:
            raise TokenError("invalid")
        return self._keys[key_id]

    async def verify(self, token: str) -> Caller:
        try:
            key_id = jwt.get_unverified_header(token).get("kid", "")
        except jwt.PyJWTError as error:
            raise TokenError("invalid") from error
        key = await self._key(str(key_id))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as error:
            raise TokenError("invalid") from error
        tenant = claims.get("tenant_id")
        # Keycloak gives no tenant to a user of a suspended tenant, or one in two (SEC-005).
        if not isinstance(tenant, str) or not tenant:
            raise TokenError("no_tenant")
        auth_time = claims.get("auth_time")
        return Caller(
            user_id=str(claims["sub"]),
            tenant_id=tenant,
            email=str(claims.get("email", "")),
            client=str(claims.get("azp", "")),
            auth_level=claims.get("acr") if isinstance(claims.get("acr"), str) else None,
            auth_time=auth_time if type(auth_time) is int else None,
        )
