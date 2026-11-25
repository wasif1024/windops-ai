"""Hear the events that may tell someone something, and keep each once (EVT-003).

Whom to tell is worked out afterwards, by the dispatcher, so hearing an event never waits on
another service. Only identifiers are kept (FR-NOT-002).

**Old events:** a new consumer replays the stream's history, and an outage may hold events back.
One that happened more than a day before it is heard tells nobody: it is kept as skipped, saying
so. A day covers any outage the platform expects to ride out (SRS 7.7).
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, RetryLaterError
from windops_migrations import in_tenant

from windops_notification import rules
from windops_notification.store import HEARD, SKIPPED, WAITING, WORK_ORDERS

SUBMITTED, DECIDED, CASES = "workorder.submitted", "approval.decided", "case.updated"
PRODUCERS = {SUBMITTED: "maintenance", DECIDED: "approval", CASES: "investigation"}
PAYLOADS = {
    SUBMITTED: events_pb2.WorkOrderSubmitted, DECIDED: events_pb2.ApprovalDecided,
    CASES: events_pb2.CaseUpdated,
}  # fmt: skip
TOO_OLD = timedelta(days=1)
DECISIONS = {
    events_pb2.APPROVAL_DECISION_APPROVED: "approved",
    events_pb2.APPROVAL_DECISION_REJECTED: "rejected",
}


def _facts(subject: str, payload: Any) -> tuple[str, dict[str, str]] | None:
    """(kind, identifiers) the event tells someone, or None when it tells nobody."""
    if subject == SUBMITTED:
        return rules.APPROVAL_REQUESTED, {
            "work_order_id": payload.work_order_id, "proposal_version": payload.proposal_version,
            "case_id": payload.case_id, "turbine_id": payload.turbine_id,
            "drafted_by": payload.drafted_by, "submitted_by": payload.submitted_by,
        }  # fmt: skip
    if subject == DECIDED:
        if payload.decision not in DECISIONS:
            raise InvalidEventError("A decision is approved or rejected")
        return rules.DECISION_MADE, {
            "work_order_id": payload.work_order_id, "proposal_version": payload.proposal_version,
            "case_id": payload.case_id, "decision": DECISIONS[payload.decision],
        }  # fmt: skip
    kind = rules.CASE_ACTIONS.get(payload.action)
    if kind is None:
        return None
    return kind, {"case_id": payload.case_id, "turbine_id": payload.turbine_id,
                  "requested_by": payload.requested_by}  # fmt: skip


async def _remember_work_order(connection: AsyncConnection, tenant: str, s: Any) -> None:
    """Who drafted and submitted it, at its newest version, for its decision."""
    w = WORK_ORDERS.c
    mine = (w.tenant_id == tenant, w.work_order_id == s.work_order_id)
    known = (await connection.execute(select(w.version).where(*mine))).scalar()
    version = int(s.proposal_version)
    values = {"version": version, "case_id": s.case_id, "turbine_id": s.turbine_id,
              "drafted_by": s.drafted_by, "submitted_by": s.submitted_by}  # fmt: skip
    if known is None:
        await connection.execute(insert(WORK_ORDERS).values(
            tenant_id=tenant, work_order_id=s.work_order_id, **values))  # fmt: skip
    elif known < version:
        await connection.execute(WORK_ORDERS.update().where(*mine).values(**values))


async def hear(engine: AsyncEngine, subject: str, event: Event,
               now: datetime | None = None) -> bool:  # fmt: skip
    """Keep one event. False for a repeat, or one that tells nobody."""
    if event.producer != PRODUCERS[subject] or not isinstance(event.payload, PAYLOADS[subject]):
        raise InvalidEventError(f"{subject} must come from {PRODUCERS[subject]}")
    payload: Any = event.payload
    if subject in (SUBMITTED, DECIDED) and not (
        payload.work_order_id and payload.proposal_version.isdigit()
    ):
        raise InvalidEventError("It needs its work order and version")
    if not payload.case_id:
        # Decisions announced before they named their case: there is nothing to say of it.
        return False
    found = _facts(subject, payload)
    if found is None:
        return False
    kind, facts = found
    now = now or datetime.now(UTC)
    stale = now - event.occurred_at > TOO_OLD
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, event.tenant_id)
            h = HEARD.c
            seen = (await connection.execute(select(h.event_id).where(
                h.tenant_id == event.tenant_id, h.event_id == event.event_id))).first()  # fmt: skip
            if seen is not None:
                return False
            if subject == SUBMITTED:
                await _remember_work_order(connection, event.tenant_id, payload)
            await connection.execute(insert(HEARD).values(
                tenant_id=event.tenant_id, event_id=event.event_id, kind=kind,
                facts=json.dumps(facts, sort_keys=True), occurred_at=event.occurred_at,
                status=SKIPPED if stale else WAITING,
                note="It happened more than a day before it was heard" if stale else "",
                next_attempt_at=now, received_at=now, updated_at=now,
            ))  # fmt: skip
    except IntegrityError as error:
        raise RetryLaterError("A concurrent change won") from error
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return not stale
