"""The tables this service owns, all tenant-owned with row-level security, and its audit records.

Each search is kept as it was made, with what was sent and what came back (EXT-003), and never
changed: a finding that cites a source must find it as it was. Each extraction is kept the same
way. The research policy is each tenant's, changed only by a person with a second factor.
"""

import hashlib
from datetime import datetime

from google.protobuf.message import Message
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
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

SERVICE_NAME = "external-intelligence"
METADATA = MetaData()

RESEARCH = Table(
    "research",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("research_id", Text, primary_key=True),
    Column("topic", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("data_gap", Text, nullable=False),
    Column("query", Text, nullable=False),
    Column("outbound_query", Text, nullable=False),
    # JSON arrays and objects.
    Column("redactions", Text, nullable=False),
    Column("wind_farm_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("region", Text, nullable=False),
    Column("turbine_class", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_request_id", Text, nullable=False),
    Column("response_seconds", Float, nullable=False),
    # Whether the provider was asked, which is what quotas count.
    Column("provider_called", Boolean, nullable=False),
    Column("sources", Text, nullable=False),
    # sha256 of the provider's answer as retrieved.
    Column("content_hash", Text, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Index("research_by_time", "tenant_id", "requested_at"),
)
EXTRACTIONS = Table(
    "extractions",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("research_id", Text, primary_key=True),
    Column("source_id", Text, primary_key=True),
    Column("attempt", Integer, primary_key=True),
    Column("url", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("flags", Text, nullable=False),
    Column("notes", Text, nullable=False),
    Column("error", Text, nullable=False),
    Column("extracted_by", Text, nullable=False),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    Index("extractions_by_time", "tenant_id", "retrieved_at"),
)
POLICIES = Table(
    "research_policies",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("allowed_domains", Text, nullable=False),
    Column("blocked_domains", Text, nullable=False),
    Column("daily_limit", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", Text, nullable=False),
)
# A retry with the same key returns the first answer, and spends nothing (GRP-007).
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
TENANT_TABLES = ("research", "extractions", "research_policies", "idempotency_keys")


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
    k = IDEMPOTENCY_KEYS.c
    row = (
        await connection.execute(
            select(IDEMPOTENCY_KEYS).where(
                k.tenant_id == tenant_id, k.user_id == user_id, k.idempotency_key == key
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
    actor_type: str,
    actor_id: str,
    action: str,
    resource_id: str,
    resource_type: str = "external_research",
    outcome: str = "succeeded",
) -> None:
    """Queue the audit record in the change's own transaction (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )
