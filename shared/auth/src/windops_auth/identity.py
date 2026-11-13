"""Signed internal identity, carried in gRPC metadata (GRP-003, GRP-004).

It says which user or service a call is for. The caller's certificate says which service made it.
"""

import contextlib
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import jwt

IDENTITY_METADATA_KEY = "x-windops-identity"
_ALGORITHM = "HS256"
_AUDIENCE = "windops-internal"
_LEEWAY_SECONDS = 5

_identity: ContextVar["VerifiedIdentity | None"] = ContextVar("windops_identity", default=None)
_token: ContextVar[str | None] = ContextVar("windops_identity_token", default=None)
_peer: ContextVar[str | None] = ContextVar("windops_peer_service", default=None)


class IdentityError(Exception):
    """The identity is missing, expired, or has an invalid signature."""


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    tenant_id: str
    user_id: str | None = None
    # How the user signed in, copied from their verified Keycloak token, for step-up (SEC-003).
    auth_level: str | None = None
    auth_time: int | None = None


def sign_identity(
    identity: VerifiedIdentity,
    secret: str,
    *,
    ttl_seconds: int = 300,
    clock: Callable[[], float] = time.time,
) -> str:
    """Create a short-lived signed identity token."""
    now = int(clock())
    claims: dict[str, str | int] = {
        "sub": identity.subject,
        "tenant_id": identity.tenant_id,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if identity.user_id is not None:
        claims["user_id"] = identity.user_id
    if identity.auth_level is not None:
        claims["acr"] = identity.auth_level
    if identity.auth_time is not None:
        claims["auth_time"] = identity.auth_time
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def verify_identity(
    token: str | None, secret: str, *, clock: Callable[[], float] = time.time
) -> VerifiedIdentity:
    """Return the identity in a token, or raise IdentityError."""
    if not token:
        raise IdentityError("Missing identity")
    try:
        # Expiry is checked below, against the injected clock. The same leeway lets a token
        # through from a caller whose clock runs a little ahead, as its "iat" is then in the
        # future here.
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "sub", "tenant_id", "aud"], "verify_exp": False},
        )
    except jwt.PyJWTError as error:
        raise IdentityError("Invalid identity") from error
    if clock() >= claims["exp"] + _LEEWAY_SECONDS:
        raise IdentityError("Expired identity")
    auth_time = claims.get("auth_time")
    return VerifiedIdentity(
        subject=claims["sub"],
        tenant_id=claims["tenant_id"],
        user_id=claims.get("user_id"),
        auth_level=claims.get("acr"),
        # Anything but a whole number of seconds is ignored, so it cannot pass as recent.
        auth_time=auth_time if type(auth_time) is int else None,
    )


def authentication_claims(identity: VerifiedIdentity) -> dict[str, object]:
    """Return the claims require_mfa and require_recent_mfa read, from an internal identity."""
    return {"acr": identity.auth_level, "auth_time": identity.auth_time}


def current_identity() -> VerifiedIdentity:
    """Return the verified identity of the current call."""
    identity = _identity.get()
    if identity is None:
        raise IdentityError("No verified identity in this context")
    return identity


def current_identity_token() -> str | None:
    """Return the current call's token, to forward on outgoing calls."""
    return _token.get()


def current_peer_service() -> str | None:
    """Return the name in the current caller's verified certificate, if it has one."""
    return _peer.get()


@contextmanager
def identity_scope(
    identity: VerifiedIdentity, token: str, *, peer_service: str | None = None
) -> Iterator[None]:
    identity_reset = _identity.set(identity)
    token_reset = _token.set(token)
    peer_reset = _peer.set(peer_service)
    try:
        yield
    finally:
        # A stream a client cancels is closed from another context, where these were never set.
        with contextlib.suppress(ValueError):
            _identity.reset(identity_reset)
        with contextlib.suppress(ValueError):
            _token.reset(token_reset)
        with contextlib.suppress(ValueError):
            _peer.reset(peer_reset)
