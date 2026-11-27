"""The tables this service owns, all tenant-owned with row-level security, and its audit records.

Evaluation is the Platform's own work: every row belongs to the Platform tenant, as only a
Platform Administrator holds evaluation.manage. The tenant column and its policy still apply, so
no other tenant could ever read one, whatever its permissions (DAT-005).
"""

import hashlib
from datetime import datetime
from typing import Any

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

SERVICE_NAME = "evaluation"
# The tenant whose Platform Administrators run evaluations, so every job's rows are its own.
PLATFORM_TENANT = "windops-platform"
METADATA = MetaData()

# Job statuses: waiting, leased, and the four outcomes.
QUEUED, RUNNING = "queued", "running"
PASSED, FAILED, INCOMPLETE, CANCELLED = "passed", "failed", "incomplete", "cancelled"
JOB_STATUSES = (QUEUED, RUNNING, PASSED, FAILED, INCOMPLETE, CANCELLED)
FINISHED = (PASSED, FAILED, INCOMPLETE, CANCELLED)
# Attempt statuses.
PLANNED, SKIPPED, ERROR = "planned", "skipped", "error"
ATTEMPT_STATUSES = (PLANNED, PASSED, FAILED, SKIPPED, ERROR)

# A dataset version, never changed once registered (EVL-005).
DATASETS = Table(
    "datasets",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("name", Text, primary_key=True),
    Column("version", Text, primary_key=True),
    Column("description", Text, nullable=False, server_default=""),
    Column("content_sha256", Text, nullable=False),
    Column("scenario_count", Integer, nullable=False),
    Column("registered_by", Text, nullable=False),
    Column("registered_at", DateTime(timezone=True), nullable=False),
)
SCENARIOS = Table(
    "dataset_scenarios",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("dataset_name", Text, primary_key=True),
    Column("dataset_version", Text, primary_key=True),
    Column("scenario_id", Text, primary_key=True),
    Column("family", Text, nullable=False),
    Column("split", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("runs_required", Integer, nullable=False),
    Column("definition_json", Text, nullable=False),
)
# A job, its lease, and once finished, its outcome and totals.
JOBS = Table(
    "evaluation_jobs",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("job_id", Text, primary_key=True),
    Column("dataset_name", Text, nullable=False),
    Column("dataset_version", Text, nullable=False),
    # Sorted and comma-joined, such as "held_out,train".
    Column("splits", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("runner", Text, nullable=False, server_default=""),
    Column("lease_token", Text, nullable=False, server_default=""),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    # A JSON object of what the runs depended on (EVL-005).
    Column("versions_json", Text, nullable=False, server_default="{}"),
    Column("note", Text, nullable=False, server_default=""),
    # The scenario whose recorded external answer the evaluation tenant gets while the lease holds
    # (EVL-001, M12.6); empty for the baseline answers.
    Column("replay_scenario", Text, nullable=False, server_default=""),
)
# Every planned attempt, and once reported, what happened.
ATTEMPTS = Table(
    "evaluation_attempts",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("job_id", Text, primary_key=True),
    Column("scenario_id", Text, primary_key=True),
    Column("attempt", Integer, primary_key=True),
    Column("status", Text, nullable=False),
    Column("run_id", Text, nullable=False, server_default=""),
    Column("case_id", Text, nullable=False, server_default=""),
    Column("rehearsed", Boolean, nullable=False, server_default="false"),
    # A serialized list of windops.evaluation.v1.Score, as reported.
    Column("scores", LargeBinary),
    Column("seconds", Float, nullable=False, server_default="0"),
    Column("tokens", BigInteger, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    Column("reason", Text, nullable=False, server_default=""),
    Column("reported_at", DateTime(timezone=True)),
)
# The job each dataset, splits and mode is compared with.
BENCHMARKS = Table(
    "benchmarks",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("dataset_name", Text, primary_key=True),
    Column("splits", Text, primary_key=True),
    Column("mode", Text, primary_key=True),
    Column("job_id", Text, nullable=False),
    Column("set_by", Text, nullable=False),
    Column("set_at", DateTime(timezone=True), nullable=False),
)
# Each run of the live suite: the real providers, through the platform's services (M12.6b). Its
# outcome is "running" until it finishes, so two replicas never run the same scheduled slot.
LIVE_RUNS = Table(
    "live_runs",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("live_tenant_id", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("checks_json", Text, nullable=False, server_default="[]"),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("triggered_by", Text, nullable=False),
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
TENANT_TABLES = (
    "datasets", "dataset_scenarios", "evaluation_jobs", "evaluation_attempts", "benchmarks",
    "idempotency_keys", "live_runs",
)  # fmt: skip


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
    resource_id: str, outcome: str = "succeeded",
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
