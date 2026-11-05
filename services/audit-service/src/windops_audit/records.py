"""Append records to a tamper-evident chain for each tenant, and check that chain."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError
from windops_migrations import in_tenant
from windops_outbox import add_event

SERVICE_NAME = "audit"
# Where each tenant's chain starts.
FIRST_PREVIOUS_HASH = "0" * 64
VERIFY_BATCH = 1000

# The migration creates this table. This definition is how the code reads and writes it.
AUDIT_RECORDS = Table(
    "audit_records",
    MetaData(),
    Column(
        "sequence",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    Column("event_id", Text, nullable=False, unique=True),
    Column("tenant_id", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("producer", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("on_behalf_of", Text, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("previous_hash", Text, nullable=False),
    Column("record_hash", Text, nullable=False),
    UniqueConstraint("tenant_id", "previous_hash", name="audit_records_one_successor"),
)


class InvalidAuditRequestError(InvalidEventError):
    """A request lacks what a record must hold, so it goes straight to the dead-letter stream."""


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    records_checked: int
    first_invalid_sequence: int


def _utc(value: datetime) -> datetime:
    """Return one form of a time, however the database returned it, so hashes stay stable."""
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def compute_hash(fields: dict[str, Any]) -> str:
    """Hash a record's content and its link to the previous record."""
    canonical = {
        key: _utc(value).isoformat() if isinstance(value, datetime) else value
        for key, value in fields.items()
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _hashed_fields(row: Any) -> dict[str, Any]:
    excluded = {"sequence", "record_hash"}
    return {str(key): value for key, value in row._mapping.items() if str(key) not in excluded}


async def append_record(connection: AsyncConnection, event: Event) -> bool:
    """Append an event's record, in the caller's transaction. Return False for a repeat."""
    await in_tenant(connection, event.tenant_id)
    existing = await connection.execute(
        select(AUDIT_RECORDS.c.sequence).where(AUDIT_RECORDS.c.event_id == event.event_id)
    )
    if existing.first() is not None:
        return False
    request = event.payload
    if not isinstance(request, events_pb2.AuditRequested):
        raise InvalidAuditRequestError("The event is not an audit request")
    if not (request.action and request.actor_id and request.outcome):
        raise InvalidAuditRequestError("An audit request needs an action, actor and outcome")
    head = await connection.execute(
        select(AUDIT_RECORDS.c.record_hash)
        .where(AUDIT_RECORDS.c.tenant_id == event.tenant_id)
        .order_by(AUDIT_RECORDS.c.sequence.desc())
        .limit(1)
    )
    previous_hash = head.scalar_one_or_none() or FIRST_PREVIOUS_HASH
    fields: dict[str, Any] = {
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "occurred_at": event.occurred_at,
        "recorded_at": datetime.now(UTC),
        "producer": event.producer,
        "action": request.action,
        "actor_type": request.actor_type,
        "actor_id": request.actor_id,
        "on_behalf_of": request.on_behalf_of,
        "resource_type": request.resource_type,
        "resource_id": request.resource_id,
        "outcome": request.outcome,
        "correlation_id": request.correlation_id or event.request_id,
        "previous_hash": previous_hash,
    }
    record_hash = compute_hash(fields)
    inserted = await connection.execute(
        AUDIT_RECORDS.insert()
        .values(**fields, record_hash=record_hash)
        .returning(AUDIT_RECORDS.c.sequence)
    )
    sequence = inserted.scalar_one()
    await add_event(
        connection,
        "audit.recorded",
        tenant_id=event.tenant_id,
        payload=events_pb2.AuditRecorded(audit_record_id=str(sequence), record_hash=record_hash),
        producer=SERVICE_NAME,
        trace_parent=event.trace_parent,
        request_id=event.request_id,
    )
    return True


async def list_records(
    connection: AsyncConnection, tenant_id: str, *, limit: int, after_sequence: int
) -> list[Any]:
    """Return one tenant's records in order, after the given sequence."""
    await in_tenant(connection, tenant_id)
    result = await connection.execute(
        select(AUDIT_RECORDS)
        .where(AUDIT_RECORDS.c.tenant_id == tenant_id, AUDIT_RECORDS.c.sequence > after_sequence)
        .order_by(AUDIT_RECORDS.c.sequence)
        .limit(limit)
    )
    return list(result.all())


async def verify_chain(connection: AsyncConnection, tenant_id: str) -> ChainVerification:
    """Recompute a tenant's chain, stopping at the first record that does not match."""
    await in_tenant(connection, tenant_id)
    expected_previous = FIRST_PREVIOUS_HASH
    checked = 0
    after = 0
    while rows := await list_records(
        connection, tenant_id, limit=VERIFY_BATCH, after_sequence=after
    ):
        for row in rows:
            if (
                row.previous_hash != expected_previous
                or compute_hash(_hashed_fields(row)) != row.record_hash
            ):
                return ChainVerification(False, checked, int(row.sequence))
            expected_previous = row.record_hash
            checked += 1
            after = int(row.sequence)
    return ChainVerification(True, checked, 0)
