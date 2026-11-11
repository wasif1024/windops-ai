"""Start the Authorization Service."""

import asyncio
import contextlib
import logging

import grpc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from windops.authorization.v1 import authorization_pb2_grpc
from windops_events import EventsUnavailableError, open_events
from windops_grpc import run_service
from windops_migrations import database_url
from windops_outbox import OutboxRelay

from windops_authorization.service import AuthorizationService
from windops_authorization.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def relay_audit_records(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish queued audit records. An outage only delays them, and must not stop the service."""
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


def main() -> None:
    engine = create_async_engine(database_url(), pool_size=5, max_overflow=5, pool_pre_ping=True)

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        authorization_pb2_grpc.add_AuthorizationServiceServicer_to_server(
            AuthorizationService(engine), server
        )

    async def background(stop: asyncio.Event) -> None:
        await relay_audit_records(engine, stop)

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
