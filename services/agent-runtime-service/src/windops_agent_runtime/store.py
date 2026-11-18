"""The tables this service owns, tenant-owned with row-level security, and its audit records.

A run is a row in `runs`, the queue itself (ARC-007). Its steps are kept as each completes, so a
run taken over by another replica carries on after its last completed step.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "agent-runtime"
ACTOR = f"service:{SERVICE_NAME}"
QUEUED, RUNNING = "queued", "running"
COMPLETED, FAILED, CANCELLED = "completed", "failed", "cancelled"
BUDGET_EXHAUSTED, STALE = "budget_exhausted", "stale"
# SRS 5.6. Once a run is in one, it never changes again.
TERMINAL = (COMPLETED, FAILED, CANCELLED, BUDGET_EXHAUSTED, STALE)
STATUSES = (QUEUED, RUNNING, *TERMINAL)
METADATA = MetaData()

RUNS = Table(
    "runs",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("case_id", Text, nullable=False),
    Column("snapshot_id", Text, nullable=False),
    # Pinned when queued (AGT-004).
    Column("workflow", Text, nullable=False),
    Column("workflow_version", Text, nullable=False),
    # Canonical JSON, as checked by the workflow.
    Column("parameters", Text, nullable=False),
    Column("total_steps", BigInteger, nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text, nullable=False, server_default=""),
    Column("attempt", BigInteger, nullable=False),
    Column("max_attempts", BigInteger, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Not claimed before this, as after a failed attempt.
    Column("not_before", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("cancel_requested_at", DateTime(timezone=True)),
    Column("cancel_requested_by", Text, nullable=False, server_default=""),
    Column("cancel_reason", Text, nullable=False, server_default=""),
    # The current claim. Every write a worker makes names its token, so a worker whose lease ran
    # out, and whose run another replica took, can change nothing.
    Column("lease_token", Text, nullable=False, server_default=""),
    Column("lease_owner", Text, nullable=False, server_default=""),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("runs_by_case", "tenant_id", "case_id", "created_at"),
    Index("runs_one_key", "tenant_id", "idempotency_key", unique=True),
    Index("runs_by_status", "status", "not_before"),
)
RUN_STEPS = Table(
    "run_steps",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("position", BigInteger, primary_key=True),
    Column("name", Text, nullable=False),
    Column("attempt", BigInteger, nullable=False),
    Column("worker", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
)


async def audit(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    actor_type: str,
    actor_id: str,
    action: str,
    run_id: str,
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
            resource_type="agent_run",
            resource_id=run_id,
            outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )
