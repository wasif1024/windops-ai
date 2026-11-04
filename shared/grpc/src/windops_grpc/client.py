"""Call other services with a deadline, trace context, identity and a breaker (GRP-003)."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
from windops_resilience import CircuitBreaker, RetryPolicy, call_with_resilience

from windops_grpc.identity import IDENTITY_METADATA_KEY, current_identity_token
from windops_grpc.interceptor import REQUEST_ID_METADATA_KEY

_tracer = trace.get_tracer("windops.grpc")


class _UnaryMethod[RequestT, ResponseT](Protocol):
    def __call__(
        self, request: RequestT, *, timeout: float, metadata: tuple[tuple[str, str], ...]
    ) -> Awaitable[ResponseT]: ...


def outgoing_metadata(
    *, request_id: str | None = None, identity_token: str | None = None
) -> tuple[tuple[str, str], ...]:
    """Build metadata for a call, forwarding the current identity and trace by default."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    carrier[REQUEST_ID_METADATA_KEY] = request_id or str(uuid.uuid4())
    token = identity_token if identity_token is not None else current_identity_token()
    if token is not None:
        carrier[IDENTITY_METADATA_KEY] = token
    return tuple(carrier.items())


class ServiceClient:
    """Calls one dependency through its circuit breaker."""

    def __init__(
        self,
        breaker: CircuitBreaker,
        *,
        identity_token: Callable[[], str | None] | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._breaker = breaker
        self._identity_token = identity_token
        self._retry = retry

    async def call[RequestT, ResponseT](
        self,
        method: _UnaryMethod[RequestT, ResponseT],
        request: RequestT,
        *,
        idempotent: bool,
        deadline_seconds: float,
    ) -> ResponseT:
        """Make a unary call. The deadline and idempotency must be chosen for each call."""
        token = self._identity_token() if self._identity_token is not None else None

        def attempt(remaining: float) -> Awaitable[ResponseT]:
            return method(
                request, timeout=remaining, metadata=outgoing_metadata(identity_token=token)
            )

        with _tracer.start_as_current_span(
            f"call {self._breaker.dependency}", kind=SpanKind.CLIENT
        ) as span:
            span.set_attribute("rpc.system", "grpc")
            span.set_attribute("peer.service", self._breaker.dependency)
            return await call_with_resilience(
                self._breaker,
                attempt,
                idempotent=idempotent,
                deadline_seconds=deadline_seconds,
                retry=self._retry,
            )
