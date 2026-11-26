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
# A person approved the case's work order; it waits for the maintenance (SRS 5.4 step 14, M10.4).
APPROVED = "approved"
# Its work is done and the turbine is watched, then its outcome waits for a person (M10.6).
VERIFYING, VERIFIED = "verifying", "verified"
CLOSED, CANCELLED = "closed", "cancelled"
ACTIVE = (OPEN, INVESTIGATING, PENDING_REVIEW, APPROVED, VERIFYING, VERIFIED)
METADATA = MetaData()
_ACTIVE_SQL = "('open', 'investigating', 'pending_review', 'approved', 'verifying', 'verified')"

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
    # Set while the newest snapshot is stale (SRS 8.5): the first rule that fired, what it found,
    # when, and the rules' version. A new snapshot clears them (EVD-008).
    Column("stale_rule", Text, nullable=False, server_default=""),
    Column("stale_detail", Text, nullable=False, server_default=""),
    Column("stale_at", DateTime(timezone=True)),
    Column("stale_rules_version", Text, nullable=False, server_default=""),
    # At most one active case from detections for a component of a turbine.
    Index(
        "cases_one_active", "tenant_id", "turbine_id", "component_group", unique=True,
        postgresql_where=text(f"origin = 'detection' AND status IN {_ACTIVE_SQL}"),
        sqlite_where=text(f"origin = 'detection' AND status IN {_ACTIVE_SQL}"),
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
# Each case's investigation, run in the Agent Runtime (M8.11, ADR 0025). One a case.
INVESTIGATIONS = Table(
    "case_investigations",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("case_id", Text, primary_key=True),
    # "waiting" for the case's evidence, "running", "completed", "ended" or "not_started".
    Column("status", Text, nullable=False),
    # "user:<id>" or "service:investigation", as the run records it.
    Column("requested_by", Text, nullable=False),
    # Empty until started, for the tenant's fault-investigator.
    Column("delegation_id", Text, nullable=False, server_default=""),
    Column("delegation_expires_at", DateTime(timezone=True)),
    Column("run_id", Text, nullable=False, server_default=""),
    Column("snapshot_id", Text, nullable=False, server_default=""),
    Column("note", Text, nullable=False, server_default=""),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("ended_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
# What happened in a case's investigation, in order, for live progress (API-003). Never changed.
PROGRESS = Table(
    "case_progress",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("case_id", Text, primary_key=True),
    # From 1 in the case: the stream's resumable event ID.
    Column("sequence", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("run_id", Text, nullable=False, server_default=""),
    Column("position", BigInteger, nullable=False, server_default="0"),
    Column("total_steps", BigInteger, nullable=False, server_default="0"),
    Column("step", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default=""),
    Column("note", Text, nullable=False, server_default=""),
    # What it records, so a replica that sees the same step again adds nothing.
    Column("event_key", Text, nullable=False),
    Index("case_progress_once", "tenant_id", "case_id", "event_key", unique=True),
)
# Each work order submitted for a decision, at its newest version: what the stale rules watch
# (SRS 8.5). Parts are "<number>=<status at submission>", procedures citations, comma-separated.
CASE_PROPOSALS = Table(
    "case_proposals",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("work_order_id", Text, primary_key=True),
    Column("version", BigInteger, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("evidence_snapshot_id", Text, nullable=False),
    Column("parts", Text, nullable=False),
    Column("procedures", Text, nullable=False),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    # The Policy Service's result at submission, and the impact estimate it judged, which the
    # weather rule asks about again (M10.3b). Empty for a proposal submitted before.
    Column("policy_result_id", Text, nullable=False, server_default=""),
    Column("impact_result_id", Text, nullable=False, server_default=""),
    Index("case_proposals_by_case", "tenant_id", "case_id"),
)
# Each person's decision on a case's work order, from approval.decided, and the run that resumes
# the case's workflow from it (SRS 5.4 step 14, M10.4, ADR 0036). One a decision.
CASE_DECISIONS = Table(
    "case_decisions",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("approval_id", Text, primary_key=True),
    Column("case_id", Text, nullable=False),
    Column("work_order_id", Text, nullable=False),
    Column("proposal_version", BigInteger, nullable=False),
    Column("evidence_snapshot_id", Text, nullable=False),
    # "approved" or "rejected".
    Column("decision", Text, nullable=False),
    Column("decided_by", Text, nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    # "waiting" to be resumed, "running", "completed", "ended" (stale, failed or cancelled), or
    # "recorded" with nothing to resume, as for a proposal no investigation made.
    Column("status", Text, nullable=False),
    # The investigation run it resumes, and the decision run that resumes it.
    Column("resumes_run_id", Text, nullable=False, server_default=""),
    Column("run_id", Text, nullable=False, server_default=""),
    Column("note", Text, nullable=False, server_default=""),
    # What the decision run recorded: identifiers, never evidence.
    Column("references_json", Text, nullable=False, server_default=""),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("case_decisions_by_case", "tenant_id", "case_id"),
)
# Each completed work order's verification (SRS 5.4 step 15, FR-MNT-005, M10.6): the telemetry
# after the work, compared with the case's evidence, and the fault rules run again. One for each
# version completed.
VERIFICATIONS = Table(
    "case_verifications",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("work_order_id", Text, primary_key=True),
    Column("proposal_version", BigInteger, primary_key=True),
    Column("case_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    # The snapshot the approved version stood on, whose telemetry is the condition before.
    Column("evidence_snapshot_id", Text, nullable=False),
    Column("completed_by", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    # "waiting", "reported" or "not_started".
    Column("status", Text, nullable=False),
    # Once reported: "resolved", "not_resolved" or "inconclusive".
    Column("outcome", Text, nullable=False, server_default=""),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("attempts", BigInteger, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("before_telemetry_snapshot_id", Text, nullable=False, server_default=""),
    Column("after_telemetry_snapshot_id", Text, nullable=False, server_default=""),
    Column("after_samples", BigInteger, nullable=False, server_default="0"),
    # The comparison and the rules' results, as JSON lists of MetricComparison and RuleResult.
    Column("metrics_json", Text, nullable=False, server_default="[]"),
    Column("rules_json", Text, nullable=False, server_default="[]"),
    Column("note", Text, nullable=False, server_default=""),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("reported_at", DateTime(timezone=True)),
    # A person's answer: "closed" or "reopened".
    Column("review", Text, nullable=False, server_default=""),
    Column("reviewed_by", Text, nullable=False, server_default=""),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("review_reason", Text, nullable=False, server_default=""),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("case_verifications_by_case", "tenant_id", "case_id"),
)
# A person's review of one finding in an investigation's report (FR-INV-006, M11.1). Never
# changed: a database trigger refuses it, and a later review is another row.
FINDING_REVIEWS = Table(
    "case_finding_reviews",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("review_id", Text, primary_key=True),
    Column("case_id", Text, nullable=False),
    Column("run_id", Text, nullable=False),
    # Where in the report, such as "hypotheses[0]".
    Column("finding", Text, nullable=False),
    # "accept", "reject", "challenge" or "annotate".
    Column("action", Text, nullable=False),
    Column("note", Text, nullable=False, server_default=""),
    Column("reviewed_by", Text, nullable=False),
    Column("reviewed_at", DateTime(timezone=True), nullable=False),
    Index("case_finding_reviews_by_case", "tenant_id", "case_id"),
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
    "evidence_items", "case_investigations", "case_progress", "case_proposals", "case_decisions",
    "case_verifications", "case_finding_reviews",
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
    actor_id: str, note: str = "", work_order_id: str = "", outcome: str = "",
) -> None:  # fmt: skip
    """Keep the change in the case's history, announce it and audit it, with it (EVT-002). A
    verification also names its work order and outcome (M10.6)."""
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
            evidence_snapshot_id=case.evidence_snapshot_id, stale_rule=case.stale_rule,
            stale_detail=case.stale_detail, stale_rules_version=case.stale_rules_version,
            requested_by=case.created_by if case.origin == "user" else "",
            work_order_id=work_order_id, verification_outcome=outcome,
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
