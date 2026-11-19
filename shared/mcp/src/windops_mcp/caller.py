"""The verified caller of the current tool call, and calls to services made for them.

An MCP server reaches a domain service as itself, by its certificate, carrying the user's verified
context: the same user and tenant, signed for no longer than their token has left. The service
then decides what that user may do, so a tool can never do more than its caller could (SRS 9.2).
"""

import contextlib
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar

from windops_auth import VerifiedIdentity, identity_scope, sign_identity
from windops_observability import tenant_context

from windops_mcp.tokens import ToolCaller

# A forwarded identity lasts at most this long, and never past the caller's token.
FORWARDED_SECONDS = 120

_caller: ContextVar[ToolCaller | None] = ContextVar("windops_mcp_caller", default=None)


class NoCallerError(RuntimeError):
    """Code that needs a verified caller ran outside a tool call."""


def current_caller() -> ToolCaller:
    caller = _caller.get()
    if caller is None:
        raise NoCallerError("No verified caller in this context")
    return caller


@contextlib.contextmanager
def caller_scope(caller: ToolCaller) -> Iterator[None]:
    reset = _caller.set(caller)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            _caller.reset(reset)


@contextlib.contextmanager
def acting_for_caller(secret: str, *, clock: Callable[[], float] = time.time) -> Iterator[None]:
    """Calls made inside carry the current caller's verified context to services."""
    caller = current_caller()
    remaining = int(caller.expires_at - clock())
    if remaining <= 0:
        raise NoCallerError("The caller's token has expired")
    # A principal with no person behind it, such as fault-investigator, goes as a service, and
    # an agent goes as the actor, so services hold it to reading and drafting (SEC-009).
    person = not caller.is_service
    identity = VerifiedIdentity(
        subject=caller.user_id,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id if person else None,
        auth_level=caller.auth_level if person else None,
        auth_time=caller.auth_time if person else None,
        actor=caller.agent or None,
    )
    token = sign_identity(identity, secret, ttl_seconds=min(FORWARDED_SECONDS, remaining))
    with identity_scope(identity, token), tenant_context(caller.tenant_id):
        yield
