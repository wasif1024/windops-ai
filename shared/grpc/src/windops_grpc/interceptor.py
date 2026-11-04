"""Apply identity, tracing, logging and error mapping to every incoming call."""

import logging
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import grpc
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind
from windops_observability import request_context

from windops_grpc.errors import DomainError
from windops_grpc.identity import (
    IDENTITY_METADATA_KEY,
    IdentityError,
    identity_scope,
    verify_identity,
)

REQUEST_ID_METADATA_KEY = "x-windops-request-id"
_HEALTH_PREFIX = "/grpc.health.v1.Health/"

logger = logging.getLogger("windops.grpc")
_tracer = trace.get_tracer("windops.grpc")


def _metadata(context: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in context.invocation_metadata() or ():
        if isinstance(value, str):
            found[key] = value
    return found


class ServerContextInterceptor(grpc.aio.ServerInterceptor):
    """Reject unverified calls, and give each call its trace, logs and error mapping."""

    def __init__(self, identity_secret: str) -> None:
        self._secret = identity_secret

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
    def _call_scope(self, method: str, context: Any) -> Iterator[Span]:
        metadata = _metadata(context)
        request_id = metadata.get(REQUEST_ID_METADATA_KEY) or str(uuid.uuid4())
        token = metadata.get(IDENTITY_METADATA_KEY)
        parent = propagate.extract(metadata)
        with (
            _tracer.start_as_current_span(method, context=parent, kind=SpanKind.SERVER) as span,
            request_context(request_id=request_id),
        ):
            span.set_attribute("rpc.system", "grpc")
            span.set_attribute("rpc.method", method)
            span.set_attribute("windops.request_id", request_id)
            identity = verify_identity(token, self._secret)
            remaining = context.time_remaining()
            if remaining is None or math.isinf(remaining):
                raise _MissingDeadlineError
            assert token is not None  # verify_identity rejects a missing token.  # noqa: S101
            with (
                identity_scope(identity, token),
                request_context(request_id=request_id, tenant_id=identity.tenant_id),
            ):
                span.set_attribute("windops.tenant_id", identity.tenant_id)
                yield span

    async def _handle_error(self, error: Exception, method: str, context: Any) -> None:
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
            logger.info("Call failed", extra={"rpc_method": method, "status": code.name})
        else:
            logger.exception("Call raised an unexpected error", extra={"rpc_method": method})
        await context.abort(code, message)

    def _wrap_unary(self, behavior: Any, method: str) -> Any:
        async def wrapped(request: Any, context: Any) -> Any:
            try:
                with self._call_scope(method, context):
                    return await behavior(request, context)
            except Exception as error:
                await self._handle_error(error, method, context)
                raise

        return wrapped

    def _wrap_stream(self, behavior: Any, method: str) -> Any:
        async def wrapped(request: Any, context: Any) -> AsyncIterator[Any]:
            try:
                with self._call_scope(method, context):
                    async for response in behavior(request, context):
                        yield response
            except Exception as error:
                await self._handle_error(error, method, context)
                raise

        return wrapped


class _MissingDeadlineError(Exception):
    """GRP-003: every call carries a deadline."""
