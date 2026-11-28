"""Start the Notification Service: its API, its event consumers, its dispatcher and its relay."""

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.notification.v1 import notification_pb2_grpc
from windops_auth import current_identity_token
from windops_events import Event, Events, EventsUnavailableError, keep_consuming, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import resilience_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_notification import consumer
from windops_notification.dispatcher import Dispatcher, keep_dispatching
from windops_notification.senders import sender
from windops_notification.service import NotificationService
from windops_notification.sources import AuthorizationClient, ServiceDirectory
from windops_notification.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5
Handler = Callable[[Event], Awaitable[Any]]


async def consume_and_relay(
    engine: AsyncEngine, stop: asyncio.Event, consumers: list[tuple[str, str, Handler]]
) -> None:
    """Read each subject on its own, and relay the outbox: audit requests. An outage must not
    restart this service (RES-006), so it keeps trying."""
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

    def handled(work: Handler) -> Callable[[Event], Awaitable[None]]:
        async def handler(event: Event) -> None:
            await work(event)

        return handler

    tasks = [asyncio.create_task(OutboxRelay(engine, events.publisher).run_forever(stop))]
    for subject, durable, work in consumers:
        made = events.consumer(subject=subject, durable=durable, handler=handled(work))
        tasks.append(asyncio.create_task(keep_consuming(made, stop, subject)))
    try:
        await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await events.close()


def _client(dependency: str) -> ServiceClient:
    breaker = CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=1.0),
        **resilience_metrics(metrics.get_meter_provider()),
    )
    # The caller's own identity goes along: a person for the API, this service for the
    # dispatcher.
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

    authorization = stub(authorization_pb2_grpc.AuthorizationServiceStub,
                         "WINDOPS_AUTHORIZATION_ADDRESS")  # fmt: skip
    directory = ServiceDirectory(
        {"authorization": authorization,
         "asset": stub(asset_pb2_grpc.AssetServiceStub, "WINDOPS_ASSET_ADDRESS")},
        {"authorization": _client("authorization"), "asset": _client("asset")},
    )  # fmt: skip
    authorizer = AuthorizationClient(authorization, _client("authorization-checks"))
    try:
        email = sender(os.environ.get("WINDOPS_NOTIFICATION_EMAIL", "record"))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    dispatcher = Dispatcher(engine, directory, email, os.environ["WINDOPS_IDENTITY_SECRET"])

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        notification_pb2_grpc.add_NotificationServiceServicer_to_server(
            NotificationService(engine, authorizer), server
        )

    def heard(subject: str) -> Handler:
        async def hear(event: Event) -> None:
            await consumer.hear(engine, subject, event)

        return hear

    async def consume(stop: asyncio.Event) -> None:
        await consume_and_relay(engine, stop, [
            (consumer.SUBMITTED, f"{SERVICE_NAME}-submissions", heard(consumer.SUBMITTED)),
            (consumer.DECIDED, f"{SERVICE_NAME}-decisions", heard(consumer.DECIDED)),
            (consumer.CASES, f"{SERVICE_NAME}-cases", heard(consumer.CASES)),
        ])  # fmt: skip

    async def dispatch(stop: asyncio.Event) -> None:
        await keep_dispatching(dispatcher, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            background=(consume, dispatch),
        )
    )


if __name__ == "__main__":
    main()
