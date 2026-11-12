"""Start the Telemetry Service: its device route over HTTPS, its gRPC API and its upkeep."""

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
from windops.telemetry.v1 import telemetry_pb2_grpc
from windops_auth import current_identity_token
from windops_cache import connect as redis_connect
from windops_events import EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_telemetry import partitions, summaries
from windops_telemetry.ingest import create_ingest_router
from windops_telemetry.limits import DeviceLimiter
from windops_telemetry.service import AssetClient, AuthorizationClient, TelemetryService
from windops_telemetry.store import SERVICE_NAME

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
    # The caller's own identity goes along, so the other service decides for them.
    return ServiceClient(breaker, identity_token=current_identity_token)


def main() -> None:
    engine = create_service_engine(pool_size=10, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    redis = redis_connect(
        host=os.environ["WINDOPS_CACHE_HOST"],
        port=int(os.environ["WINDOPS_CACHE_PORT"]),
        username=os.environ["WINDOPS_CACHE_USERNAME"],
        password=os.environ["WINDOPS_CACHE_PASSWORD"],
        ca_file=str(tls / "ca.crt"),
    )
    authorizer = _Lazy(
        lambda: AuthorizationClient(
            authorization_pb2_grpc.AuthorizationServiceStub(
                create_channel(os.environ["WINDOPS_AUTHORIZATION_ADDRESS"], tls)
            ),
            _client("authorization"),
        )
    )
    turbines = _Lazy(
        lambda: AssetClient(
            asset_pb2_grpc.AssetServiceStub(
                create_channel(os.environ["WINDOPS_ASSET_ADDRESS"], tls)
            ),
            _client("asset"),
        )
    )

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        telemetry_pb2_grpc.add_TelemetryServiceServicer_to_server(
            TelemetryService(
                engine, authorizer, turbines, version=os.environ.get("WINDOPS_VERSION", "")
            ),
            server,
        )

    async def relay(stop: asyncio.Event) -> None:
        await relay_events(engine, stop)

    async def keep_partitions(stop: asyncio.Event) -> None:
        await partitions.keep_ahead(engine, stop)

    async def keep_summaries(stop: asyncio.Event) -> None:
        await summaries.keep_summarizing(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            http_router=create_ingest_router(
                engine,
                DeviceLimiter(redis),
                raw_every_hours=int(os.environ.get("WINDOPS_TELEMETRY_CMS_RAW_EVERY_HOURS", "6")),
            ),
            # Devices reach it only over TLS, through the gateway. Plain HTTP serves health.
            api_over_https=True,
            background=(relay, keep_partitions, keep_summaries),
        )
    )


if __name__ == "__main__":
    main()
