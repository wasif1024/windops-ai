"""What this service keeps: which user a create call made, and its queued audit records."""

import hashlib
from datetime import datetime

from google.protobuf.message import Message
from sqlalchemy import Column, DateTime, MetaData, Table, Text, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "identity"
METADATA = MetaData()
# Only the user ID, never the temporary password, so a retry cannot show the password again.
CREATED_USERS = Table(
    "created_users",
    METADATA,
    Column("actor_id", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("tenant_id", Text, nullable=False),
    Column("member_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def request_hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def created_before(
    connection: AsyncConnection, actor: str, key: str, request: Message
) -> str | None:
    """Return the user a previous call with this key created, if there was one."""
    if not key:
        return None
    row = (
        await connection.execute(
            select(CREATED_USERS).where(
                CREATED_USERS.c.actor_id == actor, CREATED_USERS.c.idempotency_key == key
            )
        )
    ).one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash(request):
        raise KeyReusedError("This idempotency key was used for a different request")
    member: str = row.member_id
    return member


async def record_created(
    connection: AsyncConnection,
    *,
    actor: str,
    key: str,
    request: Message,
    tenant_id: str,
    member_id: str,
    now: datetime,
) -> None:
    if key:
        await connection.execute(
            insert(CREATED_USERS).values(
                actor_id=actor,
                idempotency_key=key,
                request_hash=request_hash(request),
                tenant_id=tenant_id,
                member_id=member_id,
                created_at=now,
            )
        )


async def audit(
    connection: AsyncConnection, *, tenant_id: str, action: str, actor_id: str, member_id: str
) -> None:
    """Queue an audit record in the caller's transaction (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action,
            actor_type="user",
            actor_id=actor_id,
            resource_type="user",
            resource_id=member_id,
            outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )
