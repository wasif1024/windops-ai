"""Start Web BFF."""

import asyncio
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
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
    state: dict[str, Any] = {}

    def tenant_client() -> tuple[Any, ServiceClient]:
        # The channel must be made on the running event loop.
        if "stub" not in state:
            channel = create_channel(os.environ["WINDOPS_TENANT_ADDRESS"], tls)
            state["stub"] = tenant_pb2_grpc.TenantServiceStub(channel)
            breaker = CircuitBreaker(
                "tenant",
                BreakerConfig(slow_call_seconds=1.0),
                on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
            )
            state["client"] = ServiceClient(breaker, identity_token=_outgoing.get)
        return state["stub"], state["client"]

    async def call_tenant(method: str, request: Any, identity: str) -> Any:
        stub, client = tenant_client()
        reset = _outgoing.set(identity)
        try:
            return await client.call(
                getattr(stub, method), request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
            )
        finally:
            _outgoing.reset(reset)

    router = create_router(
        Dependencies(
            verifier=verifier,
            limiter=RateLimiter(redis),
            identity_secret=os.environ["WINDOPS_IDENTITY_SECRET"],
            tenant=call_tenant,
        )
    )
    asyncio.run(
        run_service(
            SERVICE_NAME, register, http_router=router, http_exception_handlers=EXCEPTION_HANDLERS
        )
    )


if __name__ == "__main__":
    main()
