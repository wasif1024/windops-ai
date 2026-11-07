"""The tables this service owns, all tenant-owned with row-level security, and its audit records."""

import hashlib
from datetime import datetime

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    LargeBinary,
    MetaData,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

from windops_telemetry.schema import READINGS

SERVICE_NAME = "telemetry"
METADATA = MetaData()

DEVICE_CREDENTIALS = Table(
    "device_credentials",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("device_id", Text, primary_key=True),
    Column("turbine_id", Text, nullable=False),
    Column("secret_hash", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)
# 1 Hz samples, one row each, partitioned by event time in PostgreSQL (SRS 8.3, ADR 0003).
# Event and ingestion time are kept apart, with the source, schema version and quality status
# (FR-TEL-002). "received" until data-quality detection labels it (M4.6).
OPERATIONAL_SAMPLES = Table(
    "operational_samples",
    METADATA,
    Column("tenant_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("device_id", Text, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("source", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("synthetic", Boolean, nullable=False),
    Column("operating_state", Text, nullable=False),
    Column("available", Boolean, nullable=False),
    Column("quality_status", Text, nullable=False),
    *(Column(reading.name, Float) for reading in READINGS),
)
# What could not be stored as a sample, kept with its reason, never dropped silently (FR-TEL-003).
INGEST_REJECTIONS = Table(
    "ingest_rejections",
    METADATA,
    Column("tenant_id", Text, nullable=False),
    Column("rejection_id", Text, primary_key=True),
    Column("device_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("reason", Text, nullable=False),
    # The start of what was sent, to look at, never more than this many characters.
    Column("excerpt", Text, nullable=False),
)
IDEMPOTENCY_KEYS = Table(
    "idempotency_keys",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("user_id", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("method", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("response", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = (
    "device_credentials",
    "operational_samples",
    "ingest_rejections",
    "idempotency_keys",
)
EXCERPT_CHARACTERS = 500


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def _hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def remembered(
    connection: AsyncConnection,
    tenant_id: str,
    user_id: str,
    key: str,
    method: str,
    request: Message,
) -> bytes | None:
    """Return the first answer to a retried call, or None if this key is new."""
    if not key:
        return None
    row = (
        await connection.execute(
            select(IDEMPOTENCY_KEYS).where(
                IDEMPOTENCY_KEYS.c.tenant_id == tenant_id,
                IDEMPOTENCY_KEYS.c.user_id == user_id,
                IDEMPOTENCY_KEYS.c.idempotency_key == key,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    if row.method != method or row.request_hash != _hash(request):
        raise KeyReusedError("This idempotency key was used for a different request")
    response: bytes = row.response
    return response


async def remember(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    user_id: str,
    key: str,
    method: str,
    request: Message,
    response: Message,
    now: datetime,
) -> None:
    if key:
        await connection.execute(
            insert(IDEMPOTENCY_KEYS).values(
                tenant_id=tenant_id,
                user_id=user_id,
                idempotency_key=key,
                method=method,
                request_hash=_hash(request),
                response=response.SerializeToString(),
                created_at=now,
            )
        )


async def audit(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    actor_id: str,
    action: str,
    resource_id: str,
    actor_type: str = "user",
    outcome: str = "succeeded",
) -> None:
    """Queue an audit record in the same transaction as the change (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type="device_credential",
            resource_id=resource_id,
            outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )
