"""Start the Policy Service: its API, and the relay for its audit records."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.investigation.v1 import investigation_pb2_grpc
from windops.policy.v1 import policy_pb2_grpc
from windops.production_impact.v1 import production_impact_pb2_grpc
from windops_auth import current_identity_token
from windops_events import EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_policy.service import AuthorizationClient, PolicyService
from windops_policy.sources import ServiceSources
from windops_policy.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def relay_events(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish queued audit records. An outage only delays them (SRS 7.7)."""
    events = None
    while events is None:
        if stop.is_set():
            return
        try:
            # NATS JetStream or Azure Service Bus, as WINDOPS_ENVIRONMENT chooses (ADR 0006).
            events = await open_events(SERVICE_NAME)
        except EventsUnavailableError as error:
            logger.warning(
                "Cannot reach the events back end yet", extra={"error_type": type(error).__name__}
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    try:
        await OutboxRelay(engine, events.publisher).run_forever(stop)
    finally:
        await events.close()


class _Lazy:
    """Makes its client on first use, because a channel must be made on the running loop."""

    def __init__(self, make: Any) -> None:
        self._make, self._made = make, None

    def __getattr__(self, name: str) -> Any:
        if self._made is None:
            self._made = self._make()
        return getattr(self._made, name)


def _client(dependency: str) -> ServiceClient:
    breaker = CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=1.0),
        on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
    )
    # Whoever is current goes along: the caller, or this service acting as itself (ADR 0008).
    return ServiceClient(breaker, identity_token=current_identity_token)


def main() -> None:
    engine = create_service_engine(pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])

    def stub(make: Any, setting: str) -> Any:
        return make(create_channel(os.environ[setting], tls))

    authorizer = _Lazy(
        lambda: AuthorizationClient(
            stub(authorization_pb2_grpc.AuthorizationServiceStub, "WINDOPS_AUTHORIZATION_ADDRESS"),
            _client("authorization"),
        )
    )
    sources = _Lazy(
        lambda: ServiceSources(
            {
                "investigation": stub(investigation_pb2_grpc.InvestigationServiceStub,
                                      "WINDOPS_INVESTIGATION_ADDRESS"),
                "asset": stub(asset_pb2_grpc.AssetServiceStub, "WINDOPS_ASSET_ADDRESS"),
                "production-impact": stub(production_impact_pb2_grpc.ProductionImpactServiceStub,
                                          "WINDOPS_PRODUCTION_IMPACT_ADDRESS"),
            },
            {
                "investigation": _client("investigation"), "asset": _client("asset"),
                "production-impact": _client("production-impact"),
            },
        )
    )  # fmt: skip

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        policy_pb2_grpc.add_PolicyServiceServicer_to_server(
            PolicyService(engine, authorizer, sources, os.environ["WINDOPS_IDENTITY_SECRET"]),
            server,
        )

    async def relay(stop: asyncio.Event) -> None:
        await relay_events(engine, stop)

    asyncio.run(
        run_service(SERVICE_NAME, register, readiness_checks=(database_ready,), background=(relay,))
    )


if __name__ == "__main__":
    main()
