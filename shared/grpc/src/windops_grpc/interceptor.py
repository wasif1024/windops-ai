"""Apply identity, tracing, logging, metrics and error mapping to every incoming call."""

import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import grpc
from opentelemetry import metrics, propagate, trace
from opentelemetry.metrics import MeterProvider
from opentelemetry.trace import Span, SpanKind
from windops_observability import request_context, tenant_context

from windops_grpc.errors import DomainError
from windops_grpc.identity import (
    IDENTITY_METADATA_KEY,
    IdentityError,
    identity_scope,
    verify_identity,
)

REQUEST_ID_METADATA_KEY = "x-windops-request-id"
_HEALTH_PREFIX = "/grpc.health.v1.Health/"
_STATUS_ATTRIBUTE = "rpc.grpc.status"
# Prometheus's default buckets are too coarse for fast internal calls.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

logger = logging.getLogger("windops.grpc")
_tracer = trace.get_tracer("windops.grpc")


def _metadata(context: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in context.invocation_metadata() or ():
        if isinstance(value, str):
            found[key] = value
    return found


class _MissingDeadlineError(Exception):
    """GRP-003: every call carries a deadline."""


class ServerContextInterceptor(grpc.aio.ServerInterceptor):
    """Reject unverified calls, and give each call its trace, logs, metrics and error mapping."""

    def __init__(
        self, identity_secret: str, *, meter_provider: MeterProvider | None = None
    ) -> None:
        self._secret = identity_secret
        provider = meter_provider if meter_provider is not None else metrics.get_meter_provider()
        self._duration = provider.get_meter("windops.grpc").create_histogram(
            "windops.rpc.server.duration",
            unit="s",
            description="Time to handle an incoming gRPC call",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )

    async def intercept_service(
        self,
        continuation: Callable[[Any], Awaitable[Any]],
        handler_call_details: Any,
    ) -> Any:
        handler = await continuation(handler_call_details)
        method: str = handler_call_details.method
        if handler is None or method.startswith(_HEALTH_PREFIX):
            return handler
        return handler._replace(
            unary_unary=handler.unary_unary and self._wrap_unary(handler.unary_unary, method),
            stream_unary=handler.stream_unary and self._wrap_unary(handler.stream_unary, method),
            unary_stream=handler.unary_stream and self._wrap_stream(handler.unary_stream, method),
            stream_stream=handler.stream_stream
            and self._wrap_stream(handler.stream_stream, method),
        )

    @contextmanager
    def _call_span(self, method: str, context: Any) -> Iterator[Span]:
        """Open the call's span, which stays open for every log the call writes."""
        metadata = _metadata(context)
        request_id = metadata.get(REQUEST_ID_METADATA_KEY) or str(uuid.uuid4())
        with (
            _tracer.start_as_current_span(
                method, context=propagate.extract(metadata), kind=SpanKind.SERVER
            ) as span,
            request_context(request_id=request_id),
        ):
            span.set_attribute("rpc.system", "grpc")
            span.set_attribute("rpc.method", method)
            span.set_attribute("windops.request_id", request_id)
            yield span

    @contextmanager
    def _verified_scope(self, context: Any, span: Span) -> Iterator[None]:
        """Check identity and deadline, or raise before any service code runs."""
        metadata = _metadata(context)
        token = metadata.get(IDENTITY_METADATA_KEY)
        identity = verify_identity(token, self._secret)
        remaining = context.time_remaining()
        if remaining is None or math.isinf(remaining):
            raise _MissingDeadlineError
        assert token is not None  # verify_identity rejects a missing token.  # noqa: S101
        span.set_attribute("windops.tenant_id", identity.tenant_id)
        with (
            identity_scope(identity, token),
            tenant_context(identity.tenant_id),
        ):
            yield

    def _record(self, method: str, code: grpc.StatusCode, started: float) -> None:
        duration = time.perf_counter() - started
        self._duration.record(duration, {"rpc_method": method, "rpc_status": code.name})
        logger.info(
            "Call finished",
            extra={"rpc_method": method, "rpc_status": code.name, "duration_seconds": duration},
        )

    async def _fail(
        self, error: Exception, method: str, context: Any, span: Span, started: float
    ) -> None:
        """Abort the call with a mapped status. Unexpected details stay in the logs."""
        code, message = grpc.StatusCode.INTERNAL, "Internal error"
        if isinstance(error, IdentityError):
            code, message = grpc.StatusCode.UNAUTHENTICATED, "Invalid or missing identity"
            logger.warning("Rejected a call", extra={"rpc_method": method, "reason": str(error)})
        elif isinstance(error, _MissingDeadlineError):
            code, message = grpc.StatusCode.INVALID_ARGUMENT, "A call deadline is required"
            logger.warning("Rejected a call", extra={"rpc_method": method, "reason": message})
        elif isinstance(error, DomainError):
            code, message = error.code, str(error)
        else:
            logger.exception("Call raised an unexpected error", extra={"rpc_method": method})
        span.set_attribute(_STATUS_ATTRIBUTE, code.name)
        self._record(method, code, started)
        await context.abort(code, message)

    def _wrap_unary(self, behavior: Any, method: str) -> Any:
        async def wrapped(request: Any, context: Any) -> Any:
            started = time.perf_counter()
            with self._call_span(method, context) as span:
                try:
                    with self._verified_scope(context, span):
                        response = await behavior(request, context)
                        span.set_attribute(_STATUS_ATTRIBUTE, grpc.StatusCode.OK.name)
                        self._record(method, grpc.StatusCode.OK, started)
                        return response
                except Exception as error:
                    await self._fail(error, method, context, span, started)
                    raise

        return wrapped

    def _wrap_stream(self, behavior: Any, method: str) -> Any:
        async def wrapped(request: Any, context: Any) -> AsyncIterator[Any]:
            started = time.perf_counter()
            with self._call_span(method, context) as span:
                try:
                    with self._verified_scope(context, span):
                        async for response in behavior(request, context):
                            yield response
                        span.set_attribute(_STATUS_ATTRIBUTE, grpc.StatusCode.OK.name)
                        self._record(method, grpc.StatusCode.OK, started)
                except Exception as error:
                    await self._fail(error, method, context, span, started)
                    raise

        return wrapped
