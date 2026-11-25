"""Step 14: a person's decision resumes the case's workflow (SRS 5.4, M10.4, ADR 0036).

    approval.decided ──> waiting ──resume run started──> running ──completed──> approved, or
        │                                                    │                  still pending_review
        │                                                    └──stale or failed──> open: restart
        └──no investigation run on that snapshot──> recorded, and the case moves the same way

**Who decides:** the Approval Service, and only a person (FR-APR-001). Its decision is the system
of record; this service keeps a reference to it on the case, never a copy of its reasons.

**Resuming:** the investigation ended at step 13 with its checkpoint saved, and never waits for a
person (AGT-003). The decision is a durable event: this service, which owns the case, asks the
Agent Runtime for a `decision` run that resumes the investigation's run, as it asks for an
investigation. The runtime checks the resumed run's pins (AGT-004), restores its checkpoint and
records the references. An incompatible resume ends stale, and the case goes back to `open`,
asking a person to restart from the snapshot (AGT-005).

**The case afterwards:** approved, it is `approved`, waiting for the maintenance. Rejected, it
stays where it was, normally `pending_review`, for the proposal to be revised. Either way the
change is announced on `case.updated`, which the Notification Service tells people about (M10.5).

**Repeats and history:** each decision is kept once, by its approval ID (EVT-003). A decision on a
case this service no longer has is ignored, as is one from before decisions named their case.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, RetryLaterError
from windops_migrations import in_tenant

from windops_investigation import cases
from windops_investigation.progress import add_progress
from windops_investigation.store import (
    ACTIVE,
    APPROVED,
    CASE_DECISIONS,
    CASES,
    OPEN,
    PENDING_REVIEW,
)

SUBJECT = "approval.decided"
# Only the Approval Service may say a person decided.
PRODUCER = "approval"
WAITING, RUNNING, COMPLETED, ENDED, RECORDED = (
    "waiting",
    "running",
    "completed",
    "ended",
    "recorded",
)
DECIDED = {
    events_pb2.APPROVAL_DECISION_APPROVED: "approved",
    events_pb2.APPROVAL_DECISION_REJECTED: "rejected",
}
logger = logging.getLogger("windops.service")


async def on_decided(engine: AsyncEngine, event: Event) -> bool:
    """Keep one decision, to be resumed. False for a repeat, or one with no case to resume."""
    if event.producer != PRODUCER or not isinstance(event.payload, events_pb2.ApprovalDecided):
        raise InvalidEventError(f"{SUBJECT} must come from {PRODUCER}")
    d = event.payload
    if not d.case_id:
        # Announced before decisions named their case: nothing here can be resumed from it.
        return False
    if not d.approval_id or not d.work_order_id or not d.proposal_version.isdigit():
        raise InvalidEventError("A decision needs its approval, work order and version")
    if d.decision not in DECIDED or not d.decided_by:
        raise InvalidEventError("A decision is approved or rejected, by a person")
    try:
        async with engine.begin() as connection:
            case = await _locked(connection, event.tenant_id, d.case_id)
            if case is None:
                return False
            k = CASE_DECISIONS.c
            mine = (k.tenant_id == event.tenant_id, k.approval_id == d.approval_id)
            known = (await connection.execute(select(k.approval_id).where(*mine))).first()
            if known is not None:
                return False
            now = datetime.now(UTC)
            await connection.execute(CASE_DECISIONS.insert().values(
                tenant_id=event.tenant_id, approval_id=d.approval_id, case_id=d.case_id,
                work_order_id=d.work_order_id, proposal_version=int(d.proposal_version),
                evidence_snapshot_id=d.evidence_snapshot_id, decision=DECIDED[d.decision],
                decided_by=d.decided_by, decided_at=event.occurred_at, status=WAITING,
                received_at=now, updated_at=now,
            ))  # fmt: skip
            await add_progress(
                connection, case, key=f"decided:{d.approval_id}", kind="decided", at=now,
                status=DECIDED[d.decision],
                note=f"{d.approval_id}: work order {d.work_order_id} v{d.proposal_version}",
            )  # fmt: skip
    except IntegrityError as error:
        raise RetryLaterError("A concurrent change won") from error
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return True


async def _locked(connection: AsyncConnection, tenant: str, case_id: str) -> Any | None:
    await in_tenant(connection, tenant)
    return (await connection.execute(
        select(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
        .with_for_update()
    )).one_or_none()  # fmt: skip


async def load(connection: AsyncConnection, tenant: str, approval_id: str) -> Any | None:
    k = CASE_DECISIONS.c
    return (await connection.execute(
        select(CASE_DECISIONS).where(k.tenant_id == tenant, k.approval_id == approval_id)
    )).one_or_none()  # fmt: skip


async def set_status(
    connection: AsyncConnection, decision: Any, now: datetime, **values: Any
) -> None:
    k = CASE_DECISIONS.c
    await connection.execute(
        update(CASE_DECISIONS)
        .where(k.tenant_id == decision.tenant_id, k.approval_id == decision.approval_id)
        .values(updated_at=now, **values)
    )  # fmt: skip


def run_parameters(decision: Any, resumes: str) -> dict[str, str]:
    return {
        "resumes": resumes, "approval_id": decision.approval_id, "decision": decision.decision,
        "work_order_id": decision.work_order_id,
        "proposal_version": str(decision.proposal_version), "decided_by": decision.decided_by,
    }  # fmt: skip


async def move_case(
    connection: AsyncConnection, case: Any, decision: Any, *, now: datetime, actor_id: str,
    resumed: bool, why: str = "",
) -> Any | None:  # fmt: skip
    """Move the case on the decision, under its row lock. None when it does not move: the case
    has ended, or moved to newer evidence since the decision."""
    if case.status not in ACTIVE or case.evidence_snapshot_id != decision.evidence_snapshot_id:
        return None
    reference = (f"{decision.approval_id}: work order {decision.work_order_id} "
                 f"v{decision.proposal_version}")  # fmt: skip
    if not resumed:
        # AGT-005: the workflow cannot carry on, so a person restarts from the snapshot.
        return await cases.change(
            connection, case, now=now, action="decision_not_resumed", actor_type=cases.SERVICE,
            actor_id=actor_id, note=f"{reference}. {why}"[:1000], status=OPEN,
        )  # fmt: skip
    if decision.decision == "approved":
        return await cases.change(
            connection, case, now=now, action="approved", actor_type=cases.SERVICE,
            actor_id=actor_id, note=reference, status=APPROVED,
        )  # fmt: skip
    # Rejected: the proposal is revised as a new version, and the case waits for it.
    status = PENDING_REVIEW if case.status == APPROVED else case.status
    return await cases.change(
        connection, case, now=now, action="rejected", actor_type=cases.SERVICE,
        actor_id=actor_id, note=reference, status=status,
    )  # fmt: skip
