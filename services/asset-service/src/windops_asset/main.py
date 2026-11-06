"""Start the Asset Service: its API, and the relay that publishes its audit records and events."""

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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops_auth import current_identity_token
from windops_events import EventPublisher, connect, events_breaker
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import database_url
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_asset.service import AssetService, AuthorizationClient, Readable
from windops_asset.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def relay_events(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish queued audit records and events. An outage only delays them (SRS 7.7)."""
    client = None
    while client is None:
        if stop.is_set():
            return
        try:
            client = await connect(
                url=os.environ["WINDOPS_EVENTS_URL"],
                username=os.environ["WINDOPS_EVENTS_USERNAME"],
                password=os.environ["WINDOPS_EVENTS_PASSWORD"],
                name=SERVICE_NAME,
                ca_file=f"{os.environ['WINDOPS_TLS_DIR']}/ca.crt",
            )
        except (NatsError, APIError, TimeoutError, OSError) as error:
            logger.warning("Cannot reach JetStream yet", extra={"error_type": type(error).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    publisher = EventPublisher(client.jetstream(), producer=SERVICE_NAME, breaker=events_breaker())
    try:
        await OutboxRelay(engine, publisher).run_forever(stop)
    finally:
        await client.close()


class LazyAuthorizer:
    """Makes its client on first use, because a channel must be made on the running loop."""

    def __init__(self, address: str, tls: Path) -> None:
        self._address, self._tls = address, tls
        self._client: AuthorizationClient | None = None

    def _made(self) -> AuthorizationClient:
        if self._client is None:
            breaker = CircuitBreaker(
                "authorization",
                BreakerConfig(slow_call_seconds=1.0),
                on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
            )
            stub = authorization_pb2_grpc.AuthorizationServiceStub(
                create_channel(self._address, self._tls)
            )
            # The caller's own identity goes along, so Authorization decides for them.
            self._client = AuthorizationClient(
                stub, ServiceClient(breaker, identity_token=current_identity_token)
            )
        return self._client

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        return await self._made().allowed(permission, wind_farm_id, turbine_id)

    async def readable(self) -> Readable:
        return await self._made().readable()


def main() -> None:
    engine = create_async_engine(database_url(), pool_size=5, max_overflow=5, pool_pre_ping=True)
    authorizer: Any = LazyAuthorizer(
        os.environ["WINDOPS_AUTHORIZATION_ADDRESS"], Path(os.environ["WINDOPS_TLS_DIR"])
    )

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        asset_pb2_grpc.add_AssetServiceServicer_to_server(AssetService(engine, authorizer), server)

    async def background(stop: asyncio.Event) -> None:
        await relay_events(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            background=(background,),
        )
    )


if __name__ == "__main__":
    main()
