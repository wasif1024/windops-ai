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

from windops_maintenance.store import APPROVED, REJECTED, SUBMITTED, WORK_ORDERS, record

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
