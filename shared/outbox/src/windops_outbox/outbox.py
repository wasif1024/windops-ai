"""The outbox table, adding events in a caller's transaction, and the relay that publishes them."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from google.protobuf.message import DecodeError, Message
from opentelemetry import metrics
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import EventsUnavailableError, PublishedEvent, build_envelope

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
IDLE_SECONDS = 1.0
UNAVAILABLE_SECONDS = 5.0
STATUS_SECONDS = 15.0

_METADATA = MetaData()
OUTBOX_TABLE = Table(
    "outbox_events",
    _METADATA,
    # Rows are sent in this order.
    Column(
        "sequence",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    # Unique, so a caller repeating a request cannot queue the same event twice.
    Column("event_id", Text, nullable=False, unique=True),
    Column("subject", Text, nullable=False),
    Column("tenant_id", Text, nullable=False),
    Column("envelope", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("published_at", DateTime(timezone=True)),
    Column("failed_at", DateTime(timezone=True)),
    Column("failure_type", Text),
    # Stays small and fast however many sent rows the table holds.
    Index(
        "outbox_events_waiting",
        "sequence",
        postgresql_where=text("published_at IS NULL AND failed_at IS NULL"),
        sqlite_where=text("published_at IS NULL AND failed_at IS NULL"),
    ),
)
_WAITING = OUTBOX_TABLE.c.published_at.is_(None) & OUTBOX_TABLE.c.failed_at.is_(None)


class EnvelopePublisher(Protocol):
    async def publish_envelope(
        self, subject: str, envelope: events_pb2.EventEnvelope
    ) -> PublishedEvent: ...


@dataclass(frozen=True)
class RelayResult:
    published: int
    failed: int
    unavailable: bool


@dataclass(frozen=True)
class OutboxStatus:
    waiting: int
    failed: int
    oldest_waiting_seconds: float


def create_outbox_table(connection: Connection) -> None:
    """Create the table, from a service's migration, under that service's own login."""
    OUTBOX_TABLE.create(connection)


async def add_event(
    connection: AsyncConnection,
    subject: str,
    *,
    tenant_id: str,
    payload: Message,
    producer: str,
    event_id: str | None = None,
    trace_parent: str = "",
    request_id: str = "",
) -> str:
    """Queue an event in the caller's transaction, and return its ID."""
    if not connection.in_transaction():
        raise ValueError("add_event needs an open transaction, or it cannot be atomic (EVT-002)")
    # Checked now, so a bad event fails the state change, not the relay later.
    envelope = build_envelope(
        subject,
        tenant_id=tenant_id,
        payload=payload,
        producer=producer,
        event_id=event_id,
        trace_parent=trace_parent,
        request_id=request_id,
    )
    await connection.execute(
        insert(OUTBOX_TABLE).values(
            event_id=envelope.event_id,
            subject=subject,
            tenant_id=tenant_id,
            envelope=envelope.SerializeToString(),
        )
    )
    return envelope.event_id


def waiting_rows_query(batch: int) -> object:
    """Select unsent rows. SKIP LOCKED lets several relays take different rows (ARC-005)."""
    return (
        select(OUTBOX_TABLE.c.sequence, OUTBOX_TABLE.c.subject, OUTBOX_TABLE.c.envelope)
        .where(_WAITING)
        .order_by(OUTBOX_TABLE.c.sequence)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )


class OutboxRelay:
    """Publishes committed events, marking each only once JetStream has stored it."""

    def __init__(
        self,
        engine: AsyncEngine,
        publisher: EnvelopePublisher,
        *,
        batch: int = BATCH_SIZE,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        self._engine = engine
        self._publisher = publisher
        self._batch = batch
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("windops.outbox")
        self._waiting = meter.create_gauge(
            "windops.outbox.waiting", description="Outbox events waiting to be published"
        )
        self._failed = meter.create_gauge(
            "windops.outbox.failed",
            description="Outbox rows set aside, because they are unreadable",
        )
        self._oldest = meter.create_gauge(
            "windops.outbox.oldest_waiting_seconds",
            description="Age of the oldest outbox event waiting to be published",
        )

    async def report_status(self) -> None:
        """Export the backlog, for the Events dashboard and its alerts (SRS 7.7)."""
        async with self._engine.connect() as connection:
            status = await outbox_status(connection)
        self._waiting.set(status.waiting)
        self._failed.set(status.failed)
        self._oldest.set(status.oldest_waiting_seconds)

    async def run_once(self) -> RelayResult:
        """Send one batch, keeping what was sent if a later row cannot go."""
        published = failed = 0
        unavailable = False
        async with self._engine.begin() as connection:
            rows = (await connection.execute(waiting_rows_query(self._batch))).all()  # type: ignore[call-overload]
            for row in rows:
                try:
                    envelope = events_pb2.EventEnvelope.FromString(row.envelope)
                except DecodeError as error:
                    await self._set_aside(connection, row.sequence, error)
                    failed += 1
                    continue
                try:
                    await self._publisher.publish_envelope(row.subject, envelope)
                except EventsUnavailableError:
                    # Stop here, so no later event overtakes this one.
                    unavailable = True
                    break
                await connection.execute(
                    update(OUTBOX_TABLE)
                    .where(OUTBOX_TABLE.c.sequence == row.sequence)
                    .values(published_at=func.now())
                )
                published += 1
        return RelayResult(published, failed, unavailable)

    async def _set_aside(
        self, connection: AsyncConnection, sequence: int, error: Exception
    ) -> None:
        logger.error(
            "An outbox row cannot be read, so it was set aside",
            extra={"sequence": sequence, "error_type": type(error).__name__},
        )
        await connection.execute(
            update(OUTBOX_TABLE)
            .where(OUTBOX_TABLE.c.sequence == sequence)
            .values(failed_at=func.now(), failure_type=type(error).__name__)
        )

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Keep sending until told to stop, waiting longer while JetStream is down."""
        reported_at = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                # Before sending, and on a timer, so the backlog still shows during an outage.
                if loop.time() - reported_at >= STATUS_SECONDS:
                    await self.report_status()
                    reported_at = loop.time()
                result = await self.run_once()
            except SQLAlchemyError as error:
                # A database outage must not restart the service (RES-006).
                logger.warning(
                    "The outbox relay cannot reach the database, and will retry",
                    extra={"error_type": type(error).__name__},
                )
                result = RelayResult(0, 0, unavailable=True)
            if result.unavailable:
                delay = UNAVAILABLE_SECONDS
            elif result.published or result.failed:
                continue
            else:
                delay = IDLE_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)


async def outbox_status(connection: AsyncConnection) -> OutboxStatus:
    """Return the backlog, for the metric and alert that M2.8 adds (SRS 7.7)."""
    waiting_row = (
        await connection.execute(
            select(func.count(), func.min(OUTBOX_TABLE.c.created_at)).where(_WAITING)
        )
    ).one()
    failed = (
        await connection.execute(select(func.count()).where(OUTBOX_TABLE.c.failed_at.is_not(None)))
    ).scalar_one()
    oldest = waiting_row[1]
    age = 0.0
    if oldest is not None:
        # SQLite returns text and no zone, which only the tests use.
        stamp = datetime.fromisoformat(oldest) if isinstance(oldest, str) else oldest
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
        age = max((datetime.now(UTC) - stamp).total_seconds(), 0.0)
    return OutboxStatus(int(waiting_row[0]), int(failed), age)


async def prune_published(connection: AsyncConnection, *, older_than: timedelta) -> int:
    """Remove sent rows older than the given age, and return how many. Never touches unsent rows."""
    if older_than <= timedelta(0):
        raise ValueError("older_than must be positive")
    result = await connection.execute(
        delete(OUTBOX_TABLE).where(
            OUTBOX_TABLE.c.published_at.is_not(None),
            OUTBOX_TABLE.c.published_at < datetime.now(UTC) - older_than,
        )
    )
    return int(result.rowcount)
