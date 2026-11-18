"""The tables this service owns, and its audit records.

Each document revision is kept as uploaded, with its metadata, and never changed but for its
indexing state; its passages are kept beside it, and are what retrieval cites (M7.3). Qdrant holds
only each passage's vector and identifiers, never its text, so PostgreSQL stays the authority on
what a passage says and who may read it (SRS 8.1).

Every table but the index queue is tenant-owned, with row-level security. The queue holds only
which revision to index next, and is the service's own work list across tenants, as the outbox is.
"""

import hashlib
from datetime import datetime

from google.protobuf.message import Message
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
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "knowledge"
METADATA = MetaData()

DOCUMENTS = Table(
    "documents",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("document_id", Text, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("title", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("published", Text, nullable=False),
    Column("issued_by", Text, nullable=False),
    # JSON arrays.
    Column("applies_to", Text, nullable=False),
    Column("classification", Text, nullable=False),
    Column("supersedes_document_id", Text, nullable=False),
    Column("supersedes_revision", Integer, nullable=False),
    Column("metadata_from_document", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("filename", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("ingested_by", Text, nullable=False),
    Column("screening", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("status_reason", Text, nullable=False),
    Column("released_by", Text, nullable=False),
    Column("embedding_provider", Text, nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("embedding_version", Text, nullable=False),
    Column("indexed_at", DateTime(timezone=True)),
    Index("documents_by_status", "tenant_id", "status"),
)
PASSAGES = Table(
    "passages",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("document_id", Text, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    # The headings it sits under, joined by " > ".
    Column("section", Text, nullable=False),
    Column("text", Text, nullable=False),
    # Where it is in the document, in characters, and its 1-based first and last lines.
    Column("start_offset", Integer, nullable=False),
    Column("end_offset", Integer, nullable=False),
    Column("first_line", Integer, nullable=False),
    Column("last_line", Integer, nullable=False),
    Column("content_hash", Text, nullable=False),
)
SETTINGS = Table(
    "ingestion_settings",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("embedding_max_classification", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", Text, nullable=False),
)
INDEX_QUEUE = Table(
    "index_queue",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("document_id", Text, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Index("index_queue_due", "next_attempt_at"),
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
TENANT_TABLES = ("documents", "passages", "ingestion_settings", "idempotency_keys")


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
    resource_id: str, resource_type: str = "knowledge_document", outcome: str = "succeeded",
) -> None:  # fmt: skip
    """Queue the audit record in the change's own transaction (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action, actor_type=actor_type, actor_id=actor_id,
            resource_type=resource_type, resource_id=resource_id, outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip
