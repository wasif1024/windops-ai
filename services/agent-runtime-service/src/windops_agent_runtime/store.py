"""The tables this service owns, tenant-owned with row-level security, and its audit records.

A run is a row in `runs`, the queue itself (ARC-007). Its steps are kept as each completes, so a
run taken over by another replica carries on after its last completed step.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "agent-runtime"
ACTOR = f"service:{SERVICE_NAME}"
QUEUED, RUNNING, PAUSED = "queued", "running", "paused"
COMPLETED, FAILED, CANCELLED = "completed", "failed", "cancelled"
BUDGET_EXHAUSTED, STALE = "budget_exhausted", "stale"
# SRS 5.6. Once a run is in one, it never changes again.
TERMINAL = (COMPLETED, FAILED, CANCELLED, BUDGET_EXHAUSTED, STALE)
STATUSES = (QUEUED, RUNNING, PAUSED, *TERMINAL)
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
    # The Claude policy it runs under, and its budget, pinned when queued (AGT-004, LLM-010).
    Column("policy_version", BigInteger, nullable=False, server_default="0"),
    Column("policy_hash", Text, nullable=False, server_default=""),
    Column("token_budget", BigInteger, nullable=False, server_default="0"),
    Column("cost_budget_usd", Numeric(12, 6), nullable=False, server_default="0"),
    # The graph, prompt, tool and skill versions, as canonical JSON (AGT-004).
    Column("pins", Text, nullable=False, server_default="{}"),
    Column("pause_requested_at", DateTime(timezone=True)),
    Column("pause_requested_by", Text, nullable=False, server_default=""),
    Column("pause_reason", Text, nullable=False, server_default=""),
    # The Identity Service's delegation its agents act under, ended with the run (ADR 0019).
    Column("delegation_id", Text, nullable=False, server_default=""),
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

# Every call to Claude, as made, whatever its answer: each is paid for (LLM-010). Never changed.
RUN_CALLS = Table(
    "run_calls",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("position", BigInteger, primary_key=True),
    Column("attempt", BigInteger, nullable=False),
    Column("worker", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("requested_model", Text, nullable=False),
    Column("served_model", Text, nullable=False),
    Column("effort", Text, nullable=False),
    Column("stop_reason", Text, nullable=False),
    Column("fallback", Boolean, nullable=False),
    Column("input_tokens", BigInteger, nullable=False),
    Column("cache_write_5m_tokens", BigInteger, nullable=False),
    Column("cache_write_1h_tokens", BigInteger, nullable=False),
    Column("cache_read_tokens", BigInteger, nullable=False),
    Column("output_tokens", BigInteger, nullable=False),
    # What counts against the token budget: everything but cache reads.
    Column("processed_tokens", BigInteger, nullable=False),
    Column("cost_usd", Numeric(12, 6), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)

# LangGraph's checkpoints, one thread per run (M8.4). Written only under the run's lease, and
# never changed: a run carries on from its newest.
RUN_CHECKPOINTS = Table(
    "run_checkpoints",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("namespace", Text, primary_key=True),
    Column("checkpoint_id", Text, primary_key=True),
    Column("parent_id", Text, nullable=False),
    Column("attempt", BigInteger, nullable=False),
    Column("worker", Text, nullable=False),
    Column("checkpoint_type", Text, nullable=False),
    Column("checkpoint", LargeBinary, nullable=False),
    Column("metadata_type", Text, nullable=False),
    Column("metadata", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
# What each task wrote towards the next checkpoint, so a task that finished is not run again.
RUN_CHECKPOINT_WRITES = Table(
    "run_checkpoint_writes",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("namespace", Text, primary_key=True),
    Column("checkpoint_id", Text, primary_key=True),
    Column("task_id", Text, primary_key=True),
    Column("idx", BigInteger, primary_key=True),
    Column("channel", Text, nullable=False),
    Column("value_type", Text, nullable=False),
    Column("value", LargeBinary, nullable=False),
    Column("task_path", Text, nullable=False),
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
