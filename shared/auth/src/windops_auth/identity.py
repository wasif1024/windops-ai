"""Signed internal identity, carried in gRPC metadata (GRP-003, GRP-004).

It says which user or service a call is for. The caller's certificate says which service made it.
"""

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
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def verify_identity(
    token: str | None, secret: str, *, clock: Callable[[], float] = time.time
) -> VerifiedIdentity:
    """Return the identity in a token, or raise IdentityError."""
    if not token:
        raise IdentityError("Missing identity")
    try:
        # Expiry is checked below, against the injected clock.
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE,
            options={"require": ["exp", "sub", "tenant_id", "aud"], "verify_exp": False},
        )
    except jwt.PyJWTError as error:
        raise IdentityError("Invalid identity") from error
    if clock() >= claims["exp"] + _LEEWAY_SECONDS:
        raise IdentityError("Expired identity")
    return VerifiedIdentity(
        subject=claims["sub"], tenant_id=claims["tenant_id"], user_id=claims.get("user_id")
    )


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
        _identity.reset(identity_reset)
        _token.reset(token_reset)
        _peer.reset(peer_reset)
