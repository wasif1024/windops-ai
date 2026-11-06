"""Start Web BFF."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from windops.authorization.v1 import authorization_pb2_grpc
from windops.tenant.v1 import tenant_pb2_grpc
from windops_cache import connect as redis_connect
from windops_grpc import ServiceClient, create_channel, run_service
from windops_observability import breaker_state_metrics
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_web_bff.api import DEADLINE_SECONDS, EXCEPTION_HANDLERS, Dependencies, create_router
from windops_web_bff.limits import RateLimiter
from windops_web_bff.tokens import TokenSettings, TokenVerifier

SERVICE_NAME = "web-bff"
# The identity for the call in progress. Each request sets its own, so calls never share one.
_outgoing: ContextVar[str | None] = ContextVar("windops_bff_identity", default=None)


def register(server: grpc.aio.Server) -> None:
    """Web BFF serves the browser over HTTP. Its gRPC port carries only the health protocol."""


def main() -> None:
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    ca_file = str(tls / "ca.crt")
    verifier = TokenVerifier(
        TokenSettings(
            issuer=os.environ["WINDOPS_OIDC_ISSUER"],
            audience=os.environ["WINDOPS_OIDC_AUDIENCE"],
            jwks_url=os.environ["WINDOPS_OIDC_JWKS_URL"],
            ca_file=ca_file,
        )
    )
    redis = redis_connect(
        host=os.environ["WINDOPS_CACHE_HOST"],
        port=int(os.environ["WINDOPS_CACHE_PORT"]),
        username=os.environ["WINDOPS_CACHE_USERNAME"],
        password=os.environ["WINDOPS_CACHE_PASSWORD"],
        ca_file=ca_file,
    )
    clients: dict[str, tuple[Any, ServiceClient]] = {}
    stubs = {
        "tenant": (tenant_pb2_grpc.TenantServiceStub, "WINDOPS_TENANT_ADDRESS"),
        "authorization": (
            authorization_pb2_grpc.AuthorizationServiceStub,
            "WINDOPS_AUTHORIZATION_ADDRESS",
        ),
    }

    def client_for(service: str) -> tuple[Any, ServiceClient]:
        # The channel must be made on the running event loop.
        if service not in clients:
            stub_type, address = stubs[service]
            channel = create_channel(os.environ[address], tls)
            breaker = CircuitBreaker(
                service,
                BreakerConfig(slow_call_seconds=1.0),
                on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
            )
            clients[service] = (
                stub_type(channel),
                ServiceClient(breaker, identity_token=_outgoing.get),
            )
        return clients[service]

    def caller_for(service: str) -> Callable[[str, Any, str], Awaitable[Any]]:
        async def call(method: str, request: Any, identity: str) -> Any:
            stub, client = client_for(service)
            reset = _outgoing.set(identity)
            try:
                return await client.call(
                    getattr(stub, method),
                    request,
                    idempotent=True,
                    deadline_seconds=DEADLINE_SECONDS,
                )
            finally:
                _outgoing.reset(reset)

        return call

    router = create_router(
        Dependencies(
            verifier=verifier,
            limiter=RateLimiter(redis),
            identity_secret=os.environ["WINDOPS_IDENTITY_SECRET"],
            tenant=caller_for("tenant"),
            authorization=caller_for("authorization"),
        )
    )
    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            http_router=router,
            http_exception_handlers=EXCEPTION_HANDLERS,
            # Only over TLS. Plain HTTP keeps health and metrics, for probes and Prometheus.
            api_over_https=True,
        )
    )


if __name__ == "__main__":
    main()
