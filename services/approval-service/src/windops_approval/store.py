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
    UniqueConstraint,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "approval"
DECIDED_SUBJECT = "approval.decided"
PENDING, APPROVED, REJECTED, SUPERSEDED = "pending", "approved", "rejected", "superseded"
STATUSES = (PENDING, APPROVED, REJECTED, SUPERSEDED)
METADATA = MetaData()

# One row a request: a work order's proposal version, as submitted, and what became of it. Once
# decided or superseded, a trigger keeps it as it is (FR-APR-002).
APPROVALS = Table(
    "approvals",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("approval_id", Text, primary_key=True),
    Column("work_order_id", Text, nullable=False),
    Column("proposal_version", BigInteger, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("evidence_snapshot_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("submitted_by", Text, nullable=False),
    Column("drafted_by", Text, nullable=False, server_default=""),
    Column("drafted_by_agent", Text, nullable=False, server_default=""),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    # The event that opened it, so a repeat opens nothing (EVT-003).
    Column("source_event_id", Text, nullable=False),
    # Set once, by a decision: a serialized windops.approval.v1.Decision, and its parts that are
    # queried.
    Column("decision", LargeBinary),
    Column("decided_by", Text, nullable=False, server_default=""),
    Column("decided_at", DateTime(timezone=True)),
    Column("superseded_reason", Text, nullable=False, server_default=""),
    Column("superseded_at", DateTime(timezone=True)),
    # One request a proposal version, however often its event arrives (EVT-003).
    UniqueConstraint(
        "tenant_id", "work_order_id", "proposal_version", name="one_request_a_version"
    ),
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
TENANT_TABLES = ("approvals", "idempotency_keys")


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def _hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


def actor_of(identity: Any) -> tuple[str, str, str]:
    """(actor type, actor, on behalf of). An agent is the actor, on its principal's behalf
    (SEC-010)."""
    who = identity.user_id or identity.subject
    if identity.actor:
        return "agent", identity.actor, who
    return ("user" if identity.user_id else "service"), who, ""


async def audit(
    connection: AsyncConnection, identity: Any, *, action: str, resource_type: str,
    resource_id: str, outcome: str,
) -> None:  # fmt: skip
    """Ask the Audit Service to record it, in the same transaction (EVT-002, EVT-004)."""
    actor_type, actor_id, on_behalf_of = actor_of(identity)
    await add_event(
        connection, audit_request_subject(SERVICE_NAME), tenant_id=identity.tenant_id,
        payload=events_pb2.AuditRequested(
            action=action, actor_type=actor_type, actor_id=actor_id, on_behalf_of=on_behalf_of,
            resource_type=resource_type, resource_id=resource_id, outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip


async def denied(connection: AsyncConnection, identity: Any, permission: str) -> None:
    """Record a refusal made here, before Authorization was asked, as Authorization records its
    own, so the Denials page shows it too (SRS 9.4)."""
    await audit(connection, identity, action="authorization.denied", resource_type="permission",
                resource_id=permission, outcome="denied")  # fmt: skip


async def announce(connection: AsyncConnection, tenant_id: str, row: Any, decision: Any) -> None:
    """approval.decided, for Maintenance, the workflow and Notification. Committed with the
    decision, so a decision is never reported without it (EVT-004)."""
    await add_event(
        connection, DECIDED_SUBJECT, tenant_id=tenant_id,
        payload=events_pb2.ApprovalDecided(
            approval_id=row.approval_id, work_order_id=row.work_order_id,
            proposal_version=str(row.proposal_version),
            evidence_snapshot_id=row.evidence_snapshot_id,
            decision=(events_pb2.APPROVAL_DECISION_APPROVED if decision.outcome == APPROVED
                      else events_pb2.APPROVAL_DECISION_REJECTED),
            decided_by=decision.decided_by,
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
