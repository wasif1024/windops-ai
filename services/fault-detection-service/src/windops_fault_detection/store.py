"""The tables this service owns, all tenant-owned with row-level security, and its audit records."""

import hashlib
from datetime import datetime

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
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

SERVICE_NAME = "fault-detection"
METADATA = MetaData()

FAULT_RULES = Table(
    "fault_rules",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("rule_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("current_version", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
# Every version of every rule, never changed once written (FR-FLT-002).
FAULT_RULE_VERSIONS = Table(
    "fault_rule_versions",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("rule_id", Text, primary_key=True),
    Column("version", BigInteger, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("definition", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("reason", Text, nullable=False),
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
TENANT_TABLES = ("fault_rules", "fault_rule_versions", "idempotency_keys")


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
    """Keep the answer in the change's own transaction, so a retry finds one or neither."""
    if key:
        await connection.execute(
            insert(IDEMPOTENCY_KEYS).values(
                tenant_id=tenant_id, user_id=user_id, idempotency_key=key, method=method,
                request_hash=_hash(request), response=response.SerializeToString(),
                created_at=now,
            )
        )  # fmt: skip


async def audit(
    connection: AsyncConnection, *, tenant_id: str, actor_type: str, actor_id: str, action: str,
    rule_id: str,
) -> None:  # fmt: skip
    """Queue the audit record in the change's own transaction (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action, actor_type=actor_type, actor_id=actor_id, resource_type="fault_rule",
            resource_id=rule_id, outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip
