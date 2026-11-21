"""Open an approval request for each workorder.submitted."""

import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, RetryLaterError
from windops_migrations import in_tenant

from windops_approval.store import APPROVALS, PENDING, SUPERSEDED

SUBJECT = "workorder.submitted"
# Only the Maintenance Service may say a work order was submitted.
PRODUCER = "maintenance"
logger = logging.getLogger("windops.service")


async def handle(engine: AsyncEngine, event: Event) -> bool:
    """Open one request. False for a repeat, or a version older than one already open
    (EVT-003). An earlier version still pending is superseded by the new one."""
    if event.producer != PRODUCER or not isinstance(event.payload, events_pb2.WorkOrderSubmitted):
        raise InvalidEventError(f"{SUBJECT} must come from {PRODUCER}")
    submitted = event.payload
    fields = (submitted.work_order_id, submitted.case_id, submitted.turbine_id,
              submitted.evidence_snapshot_id, submitted.submitted_by)  # fmt: skip
    if not all(fields) or not submitted.proposal_version.isdigit():
        raise InvalidEventError("A submission needs its work order, version, case, turbine, "
                                "snapshot and submitter")  # fmt: skip
    version, now = int(submitted.proposal_version), datetime.now(UTC)
    a = APPROVALS.c
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, event.tenant_id)
            newest = (await connection.execute(
                select(a.proposal_version).where(
                    a.tenant_id == event.tenant_id, a.work_order_id == submitted.work_order_id)
                .order_by(a.proposal_version.desc()).limit(1)
            )).scalar()  # fmt: skip
            if newest is not None and newest >= version:
                return False
            await connection.execute(
                update(APPROVALS)
                .where(a.tenant_id == event.tenant_id, a.work_order_id == submitted.work_order_id,
                       a.status == PENDING)
                .values(status=SUPERSEDED, superseded_at=now,
                        superseded_reason=f"Version {version} of the work order was submitted")
            )  # fmt: skip
            await connection.execute(insert(APPROVALS).values(
                tenant_id=event.tenant_id, approval_id=f"apr_{secrets.token_hex(10)}",
                work_order_id=submitted.work_order_id, proposal_version=version,
                case_id=submitted.case_id, turbine_id=submitted.turbine_id,
                evidence_snapshot_id=submitted.evidence_snapshot_id, status=PENDING,
                submitted_by=submitted.submitted_by, drafted_by=submitted.drafted_by,
                drafted_by_agent=submitted.drafted_by_agent, submitted_at=event.occurred_at,
                requested_at=now, source_event_id=event.event_id,
            ))  # fmt: skip
    except IntegrityError as error:
        # Another replica opened it first: on the retry, it is a repeat.
        raise RetryLaterError("A concurrent change won") from error
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    logger.info("Approval requested", extra={"work_order_id": submitted.work_order_id})
    return True
