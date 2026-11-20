"""Start the Maintenance Service: its API, and the relay that publishes its outbox."""

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
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops.policy.v1 import policy_pb2_grpc
from windops_auth import current_identity_token
from windops_events import Events, EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_maintenance.service import MaintenanceService
from windops_maintenance.store import SERVICE_NAME
from windops_maintenance.validation import AuthorizationClient, ServiceSources

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def relay(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish the outbox: audit requests and workorder.submitted. An outage must not restart
    this service (RES-006), so it keeps trying to connect."""
    events: Events | None = None
    while events is None:
        if stop.is_set():
            return
        try:
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


def _client(dependency: str, slow_call_seconds: float = 1.0) -> ServiceClient:
    breaker = CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=slow_call_seconds),
        on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
    )
    # The caller's own identity goes along, so each owner decides for them.
    return ServiceClient(breaker, identity_token=current_identity_token)


def main() -> None:
    engine = create_service_engine(pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    channels: dict[str, Any] = {}

    def stub(make: Any, setting: str) -> Any:
        """A stub on a channel made on first use, on the running loop."""

        def made() -> Any:
            if setting not in channels:
                channels[setting] = create_channel(os.environ[setting], tls)
            return make(channels[setting])

        return made

    authorizer = AuthorizationClient(
        stub(authorization_pb2_grpc.AuthorizationServiceStub, "WINDOPS_AUTHORIZATION_ADDRESS"),
        _client("authorization"),
    )
    sources = ServiceSources(
        {
            "investigation": stub(
                investigation_pb2_grpc.InvestigationServiceStub, "WINDOPS_INVESTIGATION_ADDRESS"
            ),
            "asset": stub(asset_pb2_grpc.AssetServiceStub, "WINDOPS_ASSET_ADDRESS"),
            "policy": stub(policy_pb2_grpc.PolicyServiceStub, "WINDOPS_POLICY_ADDRESS"),
        },
        {
            "investigation": _client("investigation"),
            "asset": _client("asset"),
            # The Policy Service gathers a case's facts and a forecast: a long call, not a slow one.
            "policy": _client("policy", slow_call_seconds=12.0),
        },
    )

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        maintenance_pb2_grpc.add_MaintenanceServiceServicer_to_server(
            MaintenanceService(engine, authorizer, sources), server
        )

    async def publish(stop: asyncio.Event) -> None:
        await relay(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME, register, readiness_checks=(database_ready,), background=(publish,)
        )
    )


if __name__ == "__main__":
    main()
