"""Start the audit service: its read API, event consumer and outbox relay."""

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from nats.errors import Error as NatsError
from nats.js.errors import APIError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops_events import (
    AUDIT_REQUEST_SUBJECTS,
    Event,
    EventConsumer,
    EventPublisher,
    RetryLaterError,
    connect,
    events_breaker,
)
from windops_grpc import InvalidRequestError, current_identity, run_service
from windops_migrations import database_url
from windops_outbox import OutboxRelay

from windops_audit.records import SERVICE_NAME, append_record, list_records, verify_chain

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
RECONNECT_SECONDS = 2.0
# Another replica may append first. That is safe, because the database refuses a fork, so try again.
APPEND_ATTEMPTS = 5


def _timestamp(value: Any) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


class AuditService(audit_pb2_grpc.AuditServiceServicer):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def ListAuditRecords(  # noqa: N802  # The name comes from the contract.
        self, request: audit_pb2.ListAuditRecordsRequest, context: Any
    ) -> audit_pb2.ListAuditRecordsResponse:
        if request.page_size < 0:
            raise InvalidRequestError("page_size cannot be negative")
        limit = min(request.page_size or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
        try:
            after = int(request.page_token or "0")
        except ValueError as error:
            raise InvalidRequestError("page_token is not valid") from error
        async with self._engine.connect() as connection:
            rows = await list_records(
                connection, current_identity().tenant_id, limit=limit, after_sequence=after
            )
        records = [
            audit_pb2.AuditRecord(
                sequence=row.sequence,
                event_id=row.event_id,
                occurred_at=_timestamp(row.occurred_at),
                recorded_at=_timestamp(row.recorded_at),
                producer=row.producer,
                action=row.action,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                on_behalf_of=row.on_behalf_of,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                outcome=row.outcome,
                correlation_id=row.correlation_id,
                previous_hash=row.previous_hash,
                record_hash=row.record_hash,
            )
            for row in rows
        ]
        next_token = str(rows[-1].sequence) if len(rows) == limit else ""
        return audit_pb2.ListAuditRecordsResponse(records=records, next_page_token=next_token)

    async def VerifyAuditChain(  # noqa: N802  # The name comes from the contract.
        self, request: audit_pb2.VerifyAuditChainRequest, context: Any
    ) -> audit_pb2.AuditChainVerification:
        async with self._engine.connect() as connection:
            result = await verify_chain(connection, current_identity().tenant_id)
        return audit_pb2.AuditChainVerification(
            valid=result.valid,
            records_checked=result.records_checked,
            first_invalid_sequence=result.first_invalid_sequence,
        )


async def record_event(engine: AsyncEngine, event: Event) -> None:
    """Store one event's record and its announcement together, safely repeatable (EVT-003)."""
    last_conflict: IntegrityError | None = None
    for _ in range(APPEND_ATTEMPTS):
        try:
            async with engine.begin() as connection:
                await append_record(connection, event)
            return
        except IntegrityError as error:
            last_conflict = error
        except OperationalError as error:
            # The event is fine, so it waits out the outage (SRS 7.7).
            raise RetryLaterError(type(error).__name__) from error
    raise RetryLaterError("The chain stayed busy") from last_conflict


async def consume_and_relay(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Keep trying to connect, because an outage must not restart this service (RES-006)."""
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
            )
        except (NatsError, APIError, TimeoutError, OSError) as error:
            logger.warning("Cannot reach JetStream yet", extra={"error_type": type(error).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)

    js = client.jetstream()
    publisher = EventPublisher(js, producer=SERVICE_NAME, breaker=events_breaker())

    async def handler(event: Event) -> None:
        await record_event(engine, event)

    consumer = EventConsumer(
        js, subject=AUDIT_REQUEST_SUBJECTS, durable=SERVICE_NAME, handler=handler
    )
    relay = asyncio.create_task(OutboxRelay(engine, publisher).run_forever(stop))
    started = False
    try:
        while not stop.is_set():
            try:
                if not started:
                    await consumer.start()
                    started = True
                await consumer.run_once()
            except (NatsError, APIError, TimeoutError, OSError) as error:
                logger.warning(
                    "Cannot read audit requests yet", extra={"error_type": type(error).__name__}
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    finally:
        stop.set()
        await relay
        await client.close()


def registrar(engine: AsyncEngine) -> Callable[[grpc.aio.Server], None]:
    def register(server: grpc.aio.Server) -> None:
        """Add this service's gRPC servicers to the server."""
        audit_pb2_grpc.add_AuditServiceServicer_to_server(AuditService(engine), server)

    return register


def main() -> None:
    engine = create_async_engine(database_url(), pool_size=5, max_overflow=5, pool_pre_ping=True)

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    async def background(stop: asyncio.Event) -> None:
        await consume_and_relay(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            registrar(engine),
            readiness_checks=[database_ready],
            background=[background],
        )
    )


if __name__ == "__main__":
    main()
