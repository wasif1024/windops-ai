"""Start Web BFF."""

import asyncio
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from windops.alarm.v1 import alarm_pb2_grpc
from windops.approval.v1 import approval_pb2_grpc
from windops.asset.v1 import asset_pb2_grpc
from windops.audit.v1 import audit_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.identity.v1 import identity_pb2_grpc
from windops.inventory.v1 import inventory_pb2_grpc
from windops.investigation.v1 import investigation_pb2_grpc
from windops.knowledge.v1 import knowledge_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops.notification.v1 import notification_pb2_grpc
from windops.production_impact.v1 import production_impact_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2_grpc
from windops.tenant.v1 import tenant_pb2_grpc
from windops.weather.v1 import weather_pb2_grpc
from windops_cache import connect as redis_connect
from windops_grpc import ServiceClient, create_channel, outgoing_metadata, run_service
from windops_observability import breaker_state_metrics
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_web_bff.api import (
    DEADLINE_SECONDS,
    EXCEPTION_HANDLERS,
    LONG_CALLS,
    LONG_CALLS_SECONDS,
    Dependencies,
    create_router,
)
from windops_web_bff.limits import RateLimiter
from windops_web_bff.security_metrics import SecurityCounters
from windops_web_bff.tokens import TokenSettings, TokenVerifier

SERVICE_NAME = "web-bff"
# The identity for the call in progress. Each request sets its own, so calls never share one.
_outgoing: ContextVar[str | None] = ContextVar("windops_bff_identity", default=None)


def _collected(method: Callable[..., Any]) -> Callable[..., Awaitable[list[Any]]]:
    """A server-streaming method as one awaited call, so its answer passes the same breaker and
    deadline as any other. The fleet's streams are bounded, at most an hour of a few metrics."""

    async def collect(request: Any, *, timeout: float, metadata: Any) -> list[Any]:
        return [item async for item in method(request, timeout=timeout, metadata=metadata)]

    return collect


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
        "identity": (identity_pb2_grpc.IdentityServiceStub, "WINDOPS_IDENTITY_ADDRESS"),
        "audit": (audit_pb2_grpc.AuditServiceStub, "WINDOPS_AUDIT_ADDRESS"),
        "asset": (asset_pb2_grpc.AssetServiceStub, "WINDOPS_ASSET_ADDRESS"),
        "telemetry": (telemetry_pb2_grpc.TelemetryServiceStub, "WINDOPS_TELEMETRY_ADDRESS"),
        "alarm": (alarm_pb2_grpc.AlarmServiceStub, "WINDOPS_ALARM_ADDRESS"),
        "investigation": (
            investigation_pb2_grpc.InvestigationServiceStub,
            "WINDOPS_INVESTIGATION_ADDRESS",
        ),
        "maintenance": (
            maintenance_pb2_grpc.MaintenanceServiceStub,
            "WINDOPS_MAINTENANCE_ADDRESS",
        ),
        "inventory": (inventory_pb2_grpc.InventoryServiceStub, "WINDOPS_INVENTORY_ADDRESS"),
        "approval": (approval_pb2_grpc.ApprovalServiceStub, "WINDOPS_APPROVAL_ADDRESS"),
        "production_impact": (
            production_impact_pb2_grpc.ProductionImpactServiceStub,
            "WINDOPS_PRODUCTION_IMPACT_ADDRESS",
        ),
        "weather": (weather_pb2_grpc.WeatherServiceStub, "WINDOPS_WEATHER_ADDRESS"),
        "knowledge": (knowledge_pb2_grpc.KnowledgeServiceStub, "WINDOPS_KNOWLEDGE_ADDRESS"),
        "notification": (
            notification_pb2_grpc.NotificationServiceStub,
            "WINDOPS_NOTIFICATION_ADDRESS",
        ),
    }

    def client_for(service: str, long: bool = False) -> tuple[Any, ServiceClient]:
        """A service's client. A long call has one of its own, so its time never counts as
        slow against the service's ordinary calls."""
        # The channel must be made on the running event loop.
        name = f"{service}-long" if long else service
        if name not in clients:
            stub_type, address = stubs[service]
            channel = create_channel(os.environ[address], tls)
            breaker = CircuitBreaker(
                name,
                BreakerConfig(slow_call_seconds=LONG_CALLS_SECONDS if long else 1.0),
                on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
            )
            clients[name] = (
                stub_type(channel),
                ServiceClient(breaker, identity_token=_outgoing.get),
            )
        return clients[name]

    def caller_for(service: str) -> Callable[[str, Any, str], Awaitable[Any]]:
        async def call(method: str, request: Any, identity: str, *, retry: bool = True) -> Any:
            long = method in LONG_CALLS
            stub, client = client_for(service, long)
            reset = _outgoing.set(identity)
            target = getattr(stub, method)
            if method.startswith("Stream"):
                target = _collected(target)
            try:
                return await client.call(
                    target,
                    request,
                    # A change is never retried here. The browser resends it with the same key.
                    idempotent=retry,
                    deadline_seconds=LONG_CALLS_SECONDS if long else DEADLINE_SECONDS,
                )
            finally:
                _outgoing.reset(reset)

        return call

    async def watch(request: Any, identity: str, seconds: float) -> AsyncGenerator[Any]:
        """A case's progress, streamed as it happens. It bypasses the breaker, which counts
        calls, not streams that are meant to stay open; its deadline bounds it. Closing it
        cancels the call, as when the browser goes away."""
        stub, _ = client_for("investigation")
        call = stub.WatchInvestigation(
            request, timeout=seconds + 5, metadata=outgoing_metadata(identity_token=identity)
        )
        try:
            async for item in call:
                yield item
        finally:
            call.cancel()

    counters = SecurityCounters()

    async def start_counting(stop: asyncio.Event) -> None:
        # After telemetry starts, so the counts and their zeros reach Prometheus.
        counters.start()
        await stop.wait()

    router = create_router(
        Dependencies(
            verifier=verifier,
            limiter=RateLimiter(redis),
            identity_secret=os.environ["WINDOPS_IDENTITY_SECRET"],
            tenant=caller_for("tenant"),
            authorization=caller_for("authorization"),
            identity=caller_for("identity"),
            audit=caller_for("audit"),
            asset=caller_for("asset"),
            telemetry=caller_for("telemetry"),
            alarm=caller_for("alarm"),
            investigation=caller_for("investigation"),
            investigation_watch=watch,
            maintenance=caller_for("maintenance"),
            inventory=caller_for("inventory"),
            approval=caller_for("approval"),
            notification=caller_for("notification"),
            production_impact=caller_for("production_impact"),
            weather=caller_for("weather"),
            knowledge=caller_for("knowledge"),
            counters=counters,
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
            background=[start_counting],
        )
    )


if __name__ == "__main__":
    main()
