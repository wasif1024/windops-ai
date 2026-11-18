"""Who is calling a tool: a Keycloak token exchanged for this one server (MCP-001, MCP-004).

Every call carries its own bearer token, checked in full each time:

- signed by Keycloak, with the platform's issuer;
- for this server alone: a token that also names another audience, such as a user's own login,
  is too broad, and refused;
- short-lived: one issued for longer than five minutes is refused, whatever its expiry;
- for one tenant, and for a user;
- naming its tools as scopes, which each call is checked against.

The token itself is never logged, stored or handed to a tool.
"""

import asyncio
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 10
# Exchanged tokens last 120 seconds. Anything issued for longer is not one (MCP-004).
MAXIMUM_LIFETIME_SECONDS = 300
# Keycloak's keys change rarely. An unknown key ID fetches them again, at most this often.
REFRESH_SECONDS = 60

# What a caller may be told about a refused token. Only these, never an exception's own text.
REASONS = {
    "missing": "A bearer token is required",
    "invalid": "The token is not valid",
    "wrong_audience": "The token is not for this server alone",
    "too_long_lived": "The token lives too long for a tool call",
    "no_tenant": "The token carries no tenant",
    "keys_unavailable": "Tokens cannot be checked right now",
}


class TokenRefusedError(Exception):
    """The token is missing, invalid, or not one this server accepts."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in REASONS else "invalid"
        super().__init__(self.reason)

    @property
    def message(self) -> str:
        return REASONS[self.reason]


@dataclass(frozen=True)
class ToolCaller:
    """What a verified token says. A tool trusts only this."""

    user_id: str
    tenant_id: str
    scopes: frozenset[str]
    expires_at: int
    # Keycloak's ID for the token, which audit records may name. Never the token.
    token_id: str = ""
    # The agent acting for the user, when its own identity is in the token (RFC 8693 "act").
    agent: str = ""
    auth_level: str | None = None
    auth_time: int | None = None


@dataclass(frozen=True)
class TokenSettings:
    issuer: str
    # This server's own client in Keycloak, such as windops-mcp-documentation.
    audience: str
    jwks_url: str
    ca_file: str | None = None


class Verifier(Protocol):
    async def verify(self, token: str) -> ToolCaller: ...


def bearer(header: str | None) -> str:
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise TokenRefusedError("missing")
    return token.strip()


def caller_from_claims(
    claims: dict[str, Any], audience: str, *, now: float | None = None
) -> ToolCaller:
    """Everything a verified signature leaves to check, and what the token then says."""
    audiences = claims.get("aud")
    named = [audiences] if isinstance(audiences, str) else list(audiences or [])
    if named != [audience]:
        raise TokenRefusedError("wrong_audience")
    issued, expires = claims.get("iat"), claims.get("exp")
    if type(issued) is not int or type(expires) is not int:
        raise TokenRefusedError("invalid")
    if expires - issued > MAXIMUM_LIFETIME_SECONDS:
        raise TokenRefusedError("too_long_lived")
    current = time.time() if now is None else now
    not_before = claims.get("nbf", issued)
    if (
        issued > current + LEEWAY_SECONDS
        or expires <= current - LEEWAY_SECONDS
        or type(not_before) is not int
        or not_before > current + LEEWAY_SECONDS
    ):
        raise TokenRefusedError("invalid")
    tenant = claims.get("tenant_id")
    if not isinstance(tenant, str) or not tenant:
        raise TokenRefusedError("no_tenant")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise TokenRefusedError("invalid")
    scope = claims.get("scope")
    actor = claims.get("act")
    agent = actor.get("sub") if isinstance(actor, dict) else None
    auth_time = claims.get("auth_time")
    acr = claims.get("acr")
    return ToolCaller(
        user_id=subject,
        tenant_id=tenant,
        scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
        expires_at=expires,
        token_id=str(claims.get("jti", "")),
        agent=agent if isinstance(agent, str) else "",
        auth_level=acr if isinstance(acr, str) else None,
        auth_time=auth_time if type(auth_time) is int else None,
    )


class TokenVerifier:
    """Checks a token against Keycloak's published keys, fetched over verified TLS."""

    def __init__(
        self,
        settings: TokenSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._wall_clock = wall_clock
        verify: ssl.SSLContext | bool = True
        if settings.ca_file:
            verify = ssl.create_default_context(cafile=settings.ca_file)
            verify.minimum_version = ssl.TLSVersion.TLSv1_2
        self._http = httpx.AsyncClient(timeout=5.0, transport=transport, verify=verify)
        self._keys: dict[str, Any] = {}
        self._fetched: float | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    def _may_fetch(self) -> bool:
        return self._fetched is None or self._clock() - self._fetched > REFRESH_SECONDS

    async def _key(self, key_id: str) -> Any:
        if key_id not in self._keys and self._may_fetch():
            async with self._lock:
                if key_id not in self._keys and self._may_fetch():
                    try:
                        response = await self._http.get(self._settings.jwks_url)
                        response.raise_for_status()
                        published = response.json()["keys"]
                    except (httpx.HTTPError, ValueError, KeyError) as error:
                        raise TokenRefusedError("keys_unavailable") from error
                    self._fetched = self._clock()
                    self._keys = {
                        item["kid"]: jwt.PyJWK(item).key
                        for item in published
                        if item.get("use", "sig") == "sig" and item.get("alg") in ALGORITHMS
                    }
        if key_id not in self._keys:
            raise TokenRefusedError("invalid")
        return self._keys[key_id]

    async def verify(self, token: str) -> ToolCaller:
        try:
            key_id = jwt.get_unverified_header(token).get("kid", "")
        except jwt.PyJWTError as error:
            raise TokenRefusedError("invalid") from error
        key = await self._key(str(key_id))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                issuer=self._settings.issuer,
                # The audience and times are checked below, against the injected clock.
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud"],
                    "verify_aud": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.PyJWTError as error:
            raise TokenRefusedError("invalid") from error
        return caller_from_claims(claims, self._settings.audience, now=self._wall_clock())
