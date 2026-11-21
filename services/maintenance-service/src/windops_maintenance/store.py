"""The tables this service owns, all tenant-owned with row-level security, and its events."""

import hashlib
from datetime import datetime
from typing import Any

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
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

SERVICE_NAME = "maintenance"
SUBMITTED_SUBJECT = "workorder.submitted"
DRAFT, SUBMITTED, WITHDRAWN = "draft", "submitted", "withdrawn"
APPROVED, REJECTED = "approved", "rejected"
METADATA = MetaData()

# One row a work order: its current state. Every change is also kept in its history.
WORK_ORDERS = Table(
    "work_orders",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("work_order_id", Text, primary_key=True),
    Column("version", BigInteger, nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("evidence_snapshot_id", Text, nullable=False),
    # The plan and the latest validation, each a serialized protobuf message.
    Column("plan", LargeBinary, nullable=False),
    Column("validation", LargeBinary, nullable=False),
    Column("status", Text, nullable=False),
    Column("drafted_by", Text, nullable=False),
    Column("drafted_by_agent", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("submitted_by", Text, nullable=False, server_default=""),
    Column("submitted_at", DateTime(timezone=True)),
)
WORK_ORDER_HISTORY = Table(
    "work_order_history",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("work_order_id", Text, primary_key=True),
    # From 1, one a change.
    Column("position", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("action", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("agent", Text, nullable=False, server_default=""),
    Column("revision", BigInteger, nullable=False),
    Column("note", Text, nullable=False, server_default=""),
)
# A retry with the same key returns the first answer, and changes nothing (GRP-007).
IDEMPOTENCY_KEYS = Table(
    "idempotency_keys",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("caller", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("method", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("response", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = ("work_orders", "work_order_history", "idempotency_keys")


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def _hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def denied(connection: AsyncConnection, identity: Any, permission: str) -> None:
    """Record a refusal made here, before Authorization was asked, as Authorization records its
    own, so the Denials page shows it too (SRS 9.4). An agent is the actor, on its principal's
    behalf (SEC-010)."""
    actor = identity.user_id or identity.subject
    await add_event(
        connection, audit_request_subject(SERVICE_NAME), tenant_id=identity.tenant_id,
        payload=events_pb2.AuditRequested(
            action="authorization.denied", actor_type="agent" if identity.actor else (
                "user" if identity.user_id else "service"),
            actor_id=identity.actor or actor, on_behalf_of=actor if identity.actor else "",
            resource_type="permission", resource_id=permission, outcome="denied",
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip


async def remembered(
    connection: AsyncConnection, tenant_id: str, caller: str, key: str, method: str,
    request: Message,
) -> bytes | None:  # fmt: skip
    """The first answer to a retried call, or None if this key is new."""
    if not key:
        return None
    k = IDEMPOTENCY_KEYS.c
    row = (await connection.execute(
        select(IDEMPOTENCY_KEYS).where(k.tenant_id == tenant_id, k.caller == caller,
                                       k.idempotency_key == key)
    )).one_or_none()  # fmt: skip
    if row is None:
        return None
    if row.method != method or row.request_hash != _hash(request):
        raise KeyReusedError("This idempotency key was used for a different request")
    response: bytes = row.response
    return response


async def remember(
    connection: AsyncConnection, *, tenant_id: str, caller: str, key: str, method: str,
    request: Message, response: Message, now: datetime,
) -> None:  # fmt: skip
    if key:
        await connection.execute(insert(IDEMPOTENCY_KEYS).values(
            tenant_id=tenant_id, caller=caller, idempotency_key=key, method=method,
            request_hash=_hash(request), response=response.SerializeToString(), created_at=now,
        ))  # fmt: skip


async def record(
    connection: AsyncConnection, row: Any, *, action: str, at: datetime, actor_type: str,
    actor_id: str, agent: str = "", note: str = "",
) -> None:  # fmt: skip
    """Keep the change in the work order's history and audit it, in the same transaction
    (EVT-002). A submission is also announced, for Approval and Notification."""
    h = WORK_ORDER_HISTORY.c
    last = (await connection.execute(
        select(h.position).where(h.tenant_id == row.tenant_id,
                                 h.work_order_id == row.work_order_id)
        .order_by(h.position.desc()).limit(1)
    )).scalar()  # fmt: skip
    await connection.execute(insert(WORK_ORDER_HISTORY).values(
        tenant_id=row.tenant_id, work_order_id=row.work_order_id, position=(last or 0) + 1,
        at=at, action=action, actor_type=actor_type, actor_id=actor_id, agent=agent,
        revision=row.revision, note=note,
    ))  # fmt: skip
    await add_event(
        connection, audit_request_subject(SERVICE_NAME), tenant_id=row.tenant_id,
        # An agent is the actor, on its principal's behalf (SEC-010).
        payload=events_pb2.AuditRequested(
            action=f"work_order.{action}", actor_type="agent" if agent else actor_type,
            actor_id=agent or actor_id, on_behalf_of=actor_id if agent else "",
            resource_type="work_order", resource_id=row.work_order_id, outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip
    if action == "submitted":
        await add_event(
            connection,
            SUBMITTED_SUBJECT,
            tenant_id=row.tenant_id,
            payload=events_pb2.WorkOrderSubmitted(
                work_order_id=row.work_order_id,
                proposal_version=str(row.version),
                case_id=row.case_id,
                turbine_id=row.turbine_id,
                evidence_snapshot_id=row.evidence_snapshot_id,
                submitted_by=row.submitted_by,
                drafted_by=row.drafted_by,
                drafted_by_agent=row.drafted_by_agent,
            ),
            producer=SERVICE_NAME,
        )
