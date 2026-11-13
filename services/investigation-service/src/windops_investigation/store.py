"""The tables this service owns, all tenant-owned with row-level security, and its events."""

import hashlib
from datetime import datetime
from typing import Any

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    LargeBinary,
    MetaData,
    Table,
    Text,
    insert,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "investigation"
UPDATED_SUBJECT = "case.updated"
OPEN, INVESTIGATING, PENDING_REVIEW = "open", "investigating", "pending_review"
CLOSED, CANCELLED = "closed", "cancelled"
ACTIVE = (OPEN, INVESTIGATING, PENDING_REVIEW)
METADATA = MetaData()

CASES = Table(
    "cases",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("case_id", Text, primary_key=True),
    Column("turbine_id", Text, nullable=False),
    Column("component_group", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_detection_at", DateTime(timezone=True)),
    Column("case_rules_version", Text, nullable=False),
    Column("cancel_reason", Text, nullable=False, server_default=""),
    Column("version", BigInteger, nullable=False),
    Column("evidence_snapshot_id", Text, nullable=False, server_default=""),
    Column("evidence_version", BigInteger, nullable=False, server_default="0"),
    # Set while a new snapshot is wanted, as when detections join; cleared once one is frozen.
    Column("evidence_wanted_at", DateTime(timezone=True)),
    # At most one active case from detections for a component of a turbine.
    Index(
        "cases_one_active", "tenant_id", "turbine_id", "component_group", unique=True,
        postgresql_where=text(
            "origin = 'detection' AND status IN ('open', 'investigating', 'pending_review')"
        ),
        sqlite_where=text(
            "origin = 'detection' AND status IN ('open', 'investigating', 'pending_review')"
        ),
    ),
)  # fmt: skip
# Each detection, in exactly one case (EVT-003: a repeat of fault.detected changes nothing).
CASE_DETECTIONS = Table(
    "case_detections",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("detection_id", Text, primary_key=True),
    Column("case_id", Text, nullable=False),
    Column("rule_id", Text, nullable=False),
    Column("rule_version", Text, nullable=False),
    Column("rule_content_hash", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("telemetry_snapshot_id", Text, nullable=False),
    Column("attached_at", DateTime(timezone=True), nullable=False),
)
CASE_HISTORY = Table(
    "case_history",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("case_id", Text, primary_key=True),
    # The case's version after the change.
    Column("version", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("action", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("note", Text, nullable=False),
)
# A case's evidence, frozen, never changed (EVD-001): a database trigger refuses it.
EVIDENCE_SNAPSHOTS = Table(
    "evidence_snapshots",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("snapshot_id", Text, primary_key=True),
    Column("case_id", Text, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("hash_format", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", Text, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("item_count", BigInteger, nullable=False),
    Index("evidence_one_version", "tenant_id", "case_id", "version", unique=True),
)
EVIDENCE_ITEMS = Table(
    "evidence_items",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("snapshot_id", Text, primary_key=True),
    Column("position", BigInteger, primary_key=True),
    Column("source_service", Text, nullable=False),
    Column("record_type", Text, nullable=False),
    Column("record_id", Text, nullable=False),
    Column("version", Text, nullable=False),
    Column("window_start", DateTime(timezone=True)),
    Column("window_end", DateTime(timezone=True)),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    Column("data_quality", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
)
# A retry with the same key returns the first answer, and changes nothing (GRP-007).
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
    "cases", "case_detections", "case_history", "idempotency_keys", "evidence_snapshots",
    "evidence_items",
)  # fmt: skip


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def _hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def remembered(
    connection: AsyncConnection, tenant_id: str, user_id: str, key: str, method: str,
    request: Message,
) -> bytes | None:  # fmt: skip
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
    connection: AsyncConnection, *, tenant_id: str, user_id: str, key: str, method: str,
    request: Message, response: Message, now: datetime,
) -> None:  # fmt: skip
    if key:
        await connection.execute(
            insert(IDEMPOTENCY_KEYS).values(
                tenant_id=tenant_id, user_id=user_id, idempotency_key=key, method=method,
                request_hash=_hash(request), response=response.SerializeToString(),
                created_at=now,
            )
        )  # fmt: skip


async def record(
    connection: AsyncConnection, case: Any, *, action: str, at: datetime, actor_type: str,
    actor_id: str, note: str = "",
) -> None:  # fmt: skip
    """Keep the change in the case's history, announce it and audit it, with it (EVT-002)."""
    await connection.execute(
        insert(CASE_HISTORY).values(
            tenant_id=case.tenant_id, case_id=case.case_id, version=case.version, at=at,
            action=action, actor_type=actor_type, actor_id=actor_id, note=note,
        )
    )  # fmt: skip
    await add_event(
        connection,
        UPDATED_SUBJECT,
        tenant_id=case.tenant_id,
        payload=events_pb2.CaseUpdated(
            case_id=case.case_id, status=case.status, turbine_id=case.turbine_id,
            severity=case.severity, action=action, version=case.version,
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=case.tenant_id,
        payload=events_pb2.AuditRequested(
            action=f"case.{action}", actor_type=actor_type, actor_id=actor_id,
            resource_type="case", resource_id=case.case_id, outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip
