"""Apply each approval.decided to its work order: approved or rejected (SRS 5.5).

The Approval Service owns the decision; this service owns the work order, so it moves it. A
decision for any version but the one still submitted changes nothing, so a late or repeated event
is harmless (EVT-003).
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, RetryLaterError
from windops_migrations import in_tenant

from windops_maintenance.store import (
    APPROVED,
    COMPLETED,
    DRAFT,
    REJECTED,
    STALE,
    SUBMITTED,
    VERIFIED,
    WORK_ORDERS,
    record,
)

SUBJECT = "approval.decided"
# Only the Approval Service may say a work order was decided on.
PRODUCER = "approval"
OUTCOMES = {
    events_pb2.APPROVAL_DECISION_APPROVED: APPROVED,
    events_pb2.APPROVAL_DECISION_REJECTED: REJECTED,
}
logger = logging.getLogger("windops.service")


async def handle(engine: AsyncEngine, event: Event) -> bool:
    """False when nothing changed: a repeat, or a version no longer submitted."""
    if event.producer != PRODUCER or not isinstance(event.payload, events_pb2.ApprovalDecided):
        raise InvalidEventError(f"{SUBJECT} must come from {PRODUCER}")
    decided = event.payload
    status = OUTCOMES.get(decided.decision)
    if status is None or not decided.work_order_id or not decided.decided_by:
        raise InvalidEventError("A decision needs its work order, outcome and decider")
    if not decided.proposal_version.isdigit():
        raise InvalidEventError("A decision needs the proposal version it was made on")
    w, now = WORK_ORDERS.c, datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, event.tenant_id)
            mine = (w.tenant_id == event.tenant_id, w.work_order_id == decided.work_order_id)
            row = (await connection.execute(select(WORK_ORDERS).where(*mine))).one_or_none()
            if (row is None or row.status != SUBMITTED
                    or row.version != int(decided.proposal_version)):  # fmt: skip
                logger.info("A decision for a work order no longer submitted changed nothing",
                            extra={"work_order_id": decided.work_order_id})  # fmt: skip
                return False
            await connection.execute(
                update(WORK_ORDERS).where(*mine, w.status == SUBMITTED)
                .values(status=status, revision=row.revision + 1, updated_at=now)
            )  # fmt: skip
            row = (await connection.execute(select(WORK_ORDERS).where(*mine))).one()
            await record(connection, row, action=status, at=now, actor_type="user",
                         actor_id=decided.decided_by, note=decided.approval_id)  # fmt: skip
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return True


CASE_SUBJECT = "case.updated"
STALE_ACTION = "stale"
# The Investigation Service reported how the turbine ran after the work (M10.6).
VERIFIED_ACTION = "verification_reported"
OUTCOMES_REPORTED = ("resolved", "not_resolved", "inconclusive")
# Only the Investigation Service may say a case's evidence is stale.
CASE_PRODUCER = "investigation"
# What a stale snapshot makes stale: anything not yet decided, or decided but not done.
STALE_FROM = (DRAFT, SUBMITTED)


async def on_case(engine: AsyncEngine, event: Event) -> int:
    """Mark the work orders on a snapshot the stale rules found stale (SRS 8.5, EVD-007), with
    the rule, or a completed one verified. Any other case change is not for this service.
    Returns how many it marked."""
    if event.producer != CASE_PRODUCER or not isinstance(event.payload, events_pb2.CaseUpdated):
        raise InvalidEventError(f"{CASE_SUBJECT} must come from {CASE_PRODUCER}")
    case = event.payload
    if case.action == VERIFIED_ACTION:
        return await _verified(engine, event.tenant_id, case)
    if case.action != STALE_ACTION:
        return 0
    if not case.case_id or not case.evidence_snapshot_id or not case.stale_rule:
        raise InvalidEventError("A stale case names its snapshot and the rule that fired")
    w, now, marked = WORK_ORDERS.c, datetime.now(UTC), 0
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, event.tenant_id)
            rows = (await connection.execute(select(WORK_ORDERS).where(
                w.tenant_id == event.tenant_id, w.case_id == case.case_id,
                w.evidence_snapshot_id == case.evidence_snapshot_id, w.status.in_(STALE_FROM),
            ))).all()  # fmt: skip
            for row in rows:
                await connection.execute(
                    update(WORK_ORDERS)
                    .where(w.tenant_id == row.tenant_id, w.work_order_id == row.work_order_id)
                    .values(status=STALE, revision=row.revision + 1, updated_at=now,
                            stale_rule=case.stale_rule, stale_detail=case.stale_detail[:1000])
                )  # fmt: skip
                changed = (
                    await connection.execute(
                        select(WORK_ORDERS).where(
                            w.tenant_id == row.tenant_id, w.work_order_id == row.work_order_id
                        )
                    )
                ).one()
                await record(connection, changed, action=STALE, at=now, actor_type="service",
                             actor_id=f"service:{CASE_PRODUCER}",
                             note=f"{case.stale_rule}: {case.stale_detail}"[:1000])  # fmt: skip
                marked += 1
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return marked


async def _verified(engine: AsyncEngine, tenant: str, case: events_pb2.CaseUpdated) -> int:
    """Its completed work order becomes verified, with the outcome. A repeat changes nothing."""
    if not case.work_order_id or case.verification_outcome not in OUTCOMES_REPORTED:
        raise InvalidEventError("A verification names its work order and outcome")
    w, now = WORK_ORDERS.c, datetime.now(UTC)
    mine = (w.tenant_id == tenant, w.work_order_id == case.work_order_id)
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = (await connection.execute(select(WORK_ORDERS).where(*mine))).one_or_none()
            if row is None or row.status != COMPLETED or row.case_id != case.case_id:
                return 0
            await connection.execute(
                update(WORK_ORDERS).where(*mine, w.status == COMPLETED)
                .values(status=VERIFIED, revision=row.revision + 1, updated_at=now,
                        verification_outcome=case.verification_outcome)
            )  # fmt: skip
            row = (await connection.execute(select(WORK_ORDERS).where(*mine))).one()
            await record(connection, row, action=VERIFIED, at=now, actor_type="service",
                         actor_id=f"service:{CASE_PRODUCER}",
                         note=case.verification_outcome)  # fmt: skip
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return 1
