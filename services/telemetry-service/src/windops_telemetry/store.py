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
    Integer,
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
from windops_telemetry.spectra import FEATURES as CMS_FEATURE_NAMES

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
# The statistics kept for each reading in a summary.
STATISTICS = ("mean", "min", "max", "std")
# A 10-minute SCADA summary for each turbine (FR-TEL-004), partitioned by month on its period. It
# is recalculated, with a higher revision, whenever late data arrives for its period (SRS 8.3).
SCADA_SUMMARIES = Table(
    "scada_summaries",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("turbine_id", Text, primary_key=True),
    Column("period_start", DateTime(timezone=True), primary_key=True),
    Column("period_end", DateTime(timezone=True), nullable=False),
    # Distinct samples, by sequence, against the 600 a 1 Hz device sends in 10 minutes.
    Column("sample_count", Integer, nullable=False),
    Column("coverage", Float, nullable=False),
    # complete, partial or sparse, from the coverage.
    Column("quality_status", Text, nullable=False),
    Column("duplicate_samples", Integer, nullable=False),
    # Samples that arrived after the period had first been summarized.
    Column("late_samples", Integer, nullable=False),
    Column("null_readings", Integer, nullable=False),
    Column("available_fraction", Float, nullable=False),
    Column("producing_fraction", Float, nullable=False),
    Column("energy_kwh", Float),
    Column("source", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("synthetic", Boolean, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("first_computed_at", DateTime(timezone=True), nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    *(
        Column(f"{reading.name}_{statistic}", Float)
        for reading in READINGS
        for statistic in STATISTICS
    ),
)
# CMS vibration features: one row per sensor per window, every 10 minutes (FR-TEL-004). Partitioned
# by month on event time, and kept as long as the SCADA summaries.
CMS_FEATURES = Table(
    "cms_features",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("turbine_id", Text, primary_key=True),
    Column("sensor", Text, primary_key=True),
    Column("event_time", DateTime(timezone=True), primary_key=True),
    Column("sequence", BigInteger, nullable=False),
    Column("device_id", Text, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("source", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("synthetic", Boolean, nullable=False),
    # How the features were computed: windops_telemetry.spectra.FEATURE_VERSION.
    Column("feature_version", Text, nullable=False),
    Column("sample_rate_hz", Integer, nullable=False),
    Column("samples", Integer, nullable=False),
    Column("rotor_speed_rpm", Float, nullable=False),
    Column("generator_speed_rpm", Float, nullable=False),
    Column("shaft_hz", Float, nullable=False),
    Column("quality_status", Text, nullable=False),
    # The kept raw window, if there is one, and why the device flagged it, if it did.
    Column("window_id", Text),
    Column("trigger", Text),
    *(Column(name, Float) for name in CMS_FEATURE_NAMES),
)
# Raw CMS windows, kept on a schedule or a device's trigger, as float32 values (SRS 8.3).
# Partitioned by day on event time, for 90 days, or longer while a snapshot holds them.
CMS_WAVEFORMS = Table(
    "cms_waveforms",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("turbine_id", Text, primary_key=True),
    Column("sensor", Text, primary_key=True),
    Column("event_time", DateTime(timezone=True), primary_key=True),
    Column("window_id", Text, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("sample_rate_hz", Integer, nullable=False),
    Column("samples", Integer, nullable=False),
    Column("unit", Text, nullable=False),
    # "schedule", or "trigger:" and the device's reason.
    Column("kept_for", Text, nullable=False),
    Column("values_f32le", LargeBinary, nullable=False),
)
# Data a snapshot references, kept past its retention until the snapshot is released (SRS 8.3).
# Snapshots write these from M4.7. Retention keeps any partition a live hold overlaps.
RETENTION_HOLDS = Table(
    "retention_holds",
    METADATA,
    Column("tenant_id", Text, nullable=False),
    Column("hold_id", Text, primary_key=True),
    Column("table_name", Text, nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("ends_at", DateTime(timezone=True), nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True)),
)
# How far the summarizer has read, by ingestion time. Platform upkeep, not tenant data.
SUMMARY_WATERMARKS = Table(
    "summary_watermarks",
    METADATA,
    Column("name", Text, primary_key=True),
    Column("watermark", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = (
    "cms_features",
    "cms_waveforms",
    "device_credentials",
    "operational_samples",
    "ingest_rejections",
    "idempotency_keys",
    "scada_summaries",
    "retention_holds",
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
