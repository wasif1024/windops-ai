"""Start the Approval Service: its API, its workorder.submitted consumer and its relay."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import grpc
from nats.errors import Error as NatsError
from nats.js.errors import APIError
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.approval.v1 import approval_pb2_grpc
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.investigation.v1 import investigation_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops_auth import current_identity_token
from windops_events import Event, Events, EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import resilience_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_approval.consumer import SUBJECT, handle
from windops_approval.service import ApprovalService
from windops_approval.sources import AuthorizationClient, ServiceSources
from windops_approval.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def consume_and_relay(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Open a request for each submission, and publish the outbox: audit requests and
    approval.decided. An outage must not restart this service (RES-006), so it keeps trying."""
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

    async def handler(event: Event) -> None:
        await handle(engine, event)

    consumer = events.consumer(subject=SUBJECT, durable=SERVICE_NAME, handler=handler)
    relay = asyncio.create_task(OutboxRelay(engine, events.publisher).run_forever(stop))
    started = False
    try:
        while not stop.is_set():
            try:
                if not started:
                    await consumer.start()
                    started = True
                await consumer.run_once()
            except (NatsError, APIError, EventsUnavailableError, TimeoutError, OSError) as error:
                logger.warning(
                    "Cannot read submissions yet", extra={"error_type": type(error).__name__}
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    finally:
        stop.set()
        await relay
        await events.close()


def _client(dependency: str) -> ServiceClient:
    breaker = CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=1.0),
        **resilience_metrics(metrics.get_meter_provider()),
    )
    # The decider's own identity goes along, so each owner decides for them.
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
            "maintenance": stub(
                maintenance_pb2_grpc.MaintenanceServiceStub, "WINDOPS_MAINTENANCE_ADDRESS"
            ),
            "investigation": stub(
                investigation_pb2_grpc.InvestigationServiceStub, "WINDOPS_INVESTIGATION_ADDRESS"
            ),
            "asset": stub(asset_pb2_grpc.AssetServiceStub, "WINDOPS_ASSET_ADDRESS"),
        },
        {
            "maintenance": _client("maintenance"),
            "investigation": _client("investigation"),
            "asset": _client("asset"),
        },
    )

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        approval_pb2_grpc.add_ApprovalServiceServicer_to_server(
            ApprovalService(engine, authorizer, sources), server
        )

    async def consume(stop: asyncio.Event) -> None:
        await consume_and_relay(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME, register, readiness_checks=(database_ready,), background=(consume,)
        )
    )


if __name__ == "__main__":
    main()
