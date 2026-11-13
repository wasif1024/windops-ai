"""Start the Investigation Service: its API, its fault.detected consumer and its relay."""

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
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.investigation.v1 import investigation_pb2_grpc
from windops_auth import current_identity_token
from windops_events import Event, Events, EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_investigation.consumer import SUBJECT, handle
from windops_investigation.service import (
    AssetClient,
    AuthorizationClient,
    InvestigationService,
)
from windops_investigation.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def consume_and_relay(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Keep trying to connect, because an outage must not restart this service (RES-006)."""
    events: Events | None = None
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
                    "Cannot read detections yet", extra={"error_type": type(error).__name__}
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    finally:
        stop.set()
        await relay
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
    engine = create_service_engine(pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    authorizer = _Lazy(
        lambda: AuthorizationClient(
            authorization_pb2_grpc.AuthorizationServiceStub(
                create_channel(os.environ["WINDOPS_AUTHORIZATION_ADDRESS"], tls)
            ),
            _client("authorization"),
        )
    )
    assets = _Lazy(
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
        investigation_pb2_grpc.add_InvestigationServiceServicer_to_server(
            InvestigationService(engine, authorizer, assets), server
        )

    async def consume(stop: asyncio.Event) -> None:
        await consume_and_relay(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            background=(consume,),
        )
    )


if __name__ == "__main__":
    main()
