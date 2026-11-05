"""Start the sample caller, which turns an HTTP request into a gRPC call to the responder.

It exists to prove the platform end to end. Its route is public until M3.1 adds token checks at
the gateway, so it returns nothing sensitive.
"""

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any

import grpc
from fastapi import APIRouter, Query, Request, Response
from opentelemetry import metrics, propagate, trace
from opentelemetry.trace import SpanKind
from windops.sample.v1 import sample_pb2, sample_pb2_grpc
from windops_grpc import (
    ServiceClient,
    VerifiedIdentity,
    create_channel,
    run_service,
    sign_identity,
)
from windops_observability import breaker_state_metrics
from windops_resilience import BreakerConfig, BreakerOpenError, CircuitBreaker

SERVICE_NAME = "sample-caller"
DEFAULT_RESPONDER = "dns:///windops-sample-responder.windops-application.svc.cluster.local:50051"
IDENTITY = VerifiedIdentity(subject="svc-sample-caller", tenant_id="sample")
CALL_DEADLINE_SECONDS = 3.0

_tracer = trace.get_tracer("windops.sample")


def register(server: grpc.aio.Server) -> None:
    """This service answers HTTP only, so it adds no gRPC servicers."""


class Responder:
    """Calls the responder, creating its channel on the running event loop."""

    def __init__(
        self, target: str, identity_secret: str, tls_directory: Path | None = None
    ) -> None:
        self._target = target
        self._tls_directory = tls_directory
        self._secret = identity_secret
        self._stub: Any = None
        self._client: ServiceClient | None = None

    def _token(self) -> str:
        return sign_identity(IDENTITY, self._secret, ttl_seconds=60)

    def _connected(self) -> tuple[Any, ServiceClient]:
        if self._stub is None or self._client is None:
            channel = create_channel(self._target, self._tls_directory)
            self._stub = sample_pb2_grpc.SampleResponderServiceStub(channel)
            breaker = CircuitBreaker(
                "sample-responder",
                BreakerConfig(slow_call_seconds=2.0),
                on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
            )
            self._client = ServiceClient(breaker, identity_token=self._token)
        return self._stub, self._client

    async def echo(self, message: str) -> Any:
        stub, client = self._connected()
        return await client.call(
            stub.Echo,
            sample_pb2.EchoRequest(message=message),
            idempotent=True,
            deadline_seconds=CALL_DEADLINE_SECONDS,
        )


def create_router(responder: Responder) -> APIRouter:
    router = APIRouter()

    @router.get("/sample/echo")
    async def echo(
        request: Request,
        response: Response,
        message: Annotated[str, Query(min_length=1, max_length=200)],
    ) -> dict[str, str]:
        parent = propagate.extract(dict(request.headers))
        with _tracer.start_as_current_span(
            "GET /sample/echo", context=parent, kind=SpanKind.SERVER
        ) as span:
            try:
                reply = await responder.echo(message)
            except BreakerOpenError:
                # SRS 7.7: fail at once, without calling a dependency that is known to be down.
                span.set_attribute("windops.result", "breaker_open")
                response.status_code = 503
                return {"error": "The responder is unavailable", "reason": "breaker_open"}
            except (grpc.aio.AioRpcError, TimeoutError):
                span.set_attribute("windops.result", "call_failed")
                response.status_code = 503
                return {"error": "The responder is unavailable", "reason": "call_failed"}
            return {"message": reply.message, "tenant_id": reply.tenant_id}

    return router


def main() -> None:
    responder = Responder(
        os.environ.get("WINDOPS_SAMPLE_RESPONDER_ADDRESS", DEFAULT_RESPONDER),
        os.environ.get("WINDOPS_IDENTITY_SECRET", ""),
        Path(os.environ["WINDOPS_TLS_DIR"]) if os.environ.get("WINDOPS_TLS_DIR") else None,
    )
    asyncio.run(run_service(SERVICE_NAME, register, http_router=create_router(responder)))


if __name__ == "__main__":
    main()
