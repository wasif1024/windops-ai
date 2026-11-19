"""Who is calling a tool: a token for this one server (MCP-001, MCP-004).

Two issuers are trusted, each for its own kind of caller:
- **Keycloak,** for a person's own token, exchanged for this server. It never names an agent.
- **The Identity Service,** for an agent acting for a person or for the tenant's
  fault-investigator (ADR 0019). It must name the agent in "act", as "agent:<name>". Its keys
  are fetched from the Identity Service over mutual TLS.

Every call carries its own bearer token, checked in full each time:

- signed by its issuer, with that issuer's own keys and algorithm;
- for this server alone: a token that also names another audience, such as a user's own login,
  is too broad, and refused;
- short-lived: one issued for longer than five minutes is refused, whatever its expiry;
- for one tenant, and for a user;
- naming its tools as scopes, which each call is checked against.

The token itself is never logged, stored or handed to a tool.
"""

import asyncio
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt

ALGORITHMS = ["RS256"]
AGENT_ALGORITHMS = ["ES256"]
AGENT_PREFIX = "agent:"
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
    # The run an agent's call is for, shared with its trace (OBS-008). Empty for a person.
    correlation_id: str = ""

    @property
    def is_service(self) -> bool:
        """The subject is a principal with no person behind it, such as fault-investigator."""
        return self.user_id.startswith("service:")


@dataclass(frozen=True)
class TokenSettings:
    issuer: str
    # This server's own client in Keycloak, such as windops-mcp-documentation.
    audience: str
    jwks_url: str
    ca_file: str | None = None


@dataclass(frozen=True)
class AgentIssuer:
    """The Identity Service, which signs agents' tokens (ADR 0019)."""

    issuer: str
    # Returns its published keys, a JWKS's "keys".
    fetch_keys: Callable[[], Awaitable[list[dict[str, Any]]]]


class _Keys:
    """One issuer's signing keys, by key ID, fetched again for an unknown ID at most once a
    minute."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[list[dict[str, Any]]]],
        algorithms: list[str],
        clock: Callable[[], float],
    ) -> None:
        self._fetch, self.algorithms, self._clock = fetch, algorithms, clock
        self._keys: dict[str, Any] = {}
        self._fetched: float | None = None
        self._lock = asyncio.Lock()

    def _may_fetch(self) -> bool:
        return self._fetched is None or self._clock() - self._fetched > REFRESH_SECONDS

    async def key(self, key_id: str) -> Any:
        if key_id not in self._keys and self._may_fetch():
            async with self._lock:
                if key_id not in self._keys and self._may_fetch():
                    try:
                        published = await self._fetch()
                    except Exception as error:
                        raise TokenRefusedError("keys_unavailable") from error
                    self._fetched = self._clock()
                    self._keys = {
                        item["kid"]: jwt.PyJWK(item).key
                        for item in published
                        if item.get("use", "sig") == "sig" and item.get("alg") in self.algorithms
                    }
        if key_id not in self._keys:
            raise TokenRefusedError("invalid")
        return self._keys[key_id]


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
    correlation = claims.get("cid")
    return ToolCaller(
        user_id=subject,
        tenant_id=tenant,
        scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
        expires_at=expires,
        token_id=str(claims.get("jti", "")),
        agent=agent if isinstance(agent, str) else "",
        auth_level=acr if isinstance(acr, str) else None,
        auth_time=auth_time if type(auth_time) is int else None,
        correlation_id=correlation
        if isinstance(correlation, str) and re.fullmatch(r"[a-z0-9_]{1,64}", correlation)
        else "",
    )


class TokenVerifier:
    """Checks a token against its issuer's published keys: Keycloak's, fetched over verified TLS,
    or, for agents, the Identity Service's."""

    def __init__(
        self,
        settings: TokenSettings,
        *,
        agents: AgentIssuer | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._agents = agents
        self._wall_clock = wall_clock
        verify: ssl.SSLContext | bool = True
        if settings.ca_file:
            verify = ssl.create_default_context(cafile=settings.ca_file)
            verify.minimum_version = ssl.TLSVersion.TLSv1_2
        self._http = httpx.AsyncClient(timeout=5.0, transport=transport, verify=verify)
        self._keycloak = _Keys(self._keycloak_keys, ALGORITHMS, clock)
        self._agent_keys = _Keys(agents.fetch_keys, AGENT_ALGORITHMS, clock) if agents else None

    async def _keycloak_keys(self) -> list[dict[str, Any]]:
        response = await self._http.get(self._settings.jwks_url)
        response.raise_for_status()
        keys: list[dict[str, Any]] = response.json()["keys"]
        return keys

    async def close(self) -> None:
        await self._http.aclose()

    async def verify(self, token: str) -> ToolCaller:
        try:
            key_id = str(jwt.get_unverified_header(token).get("kid", ""))
            # Only to choose whose keys to check it with: the signature is checked next.
            issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError as error:
            raise TokenRefusedError("invalid") from error
        if issuer == self._settings.issuer:
            keys = self._keycloak
        elif self._agents is not None and self._agent_keys and issuer == self._agents.issuer:
            keys = self._agent_keys
        else:
            raise TokenRefusedError("invalid")
        key = await keys.key(key_id)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=keys.algorithms,
                issuer=issuer,
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
        actor = claims.get("act")
        agent = actor.get("sub") if isinstance(actor, dict) else None
        if keys is self._keycloak and actor is not None:
            # Only the Identity Service names agents: a person's own token never does.
            raise TokenRefusedError("invalid")
        if keys is self._agent_keys and not (
            isinstance(agent, str) and agent.startswith(AGENT_PREFIX) and len(agent) > 6
        ):
            raise TokenRefusedError("invalid")
        return caller_from_claims(claims, self._settings.audience, now=self._wall_clock())
