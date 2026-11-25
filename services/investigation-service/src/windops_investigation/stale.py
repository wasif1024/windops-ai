"""Apply the stale rules (staleness.py) to what happens: alarms, changed evidence, submitted
proposals, and the passing of time (SRS 8.5).

Each handler is safe to run twice for the same event (EVT-003): a case already stale stays as it
is, and a proposal is kept only at its newest version. What must be asked of another owner, the
turbine or a part's stock, is asked as this service, and a failure retries the event later,
never deciding without it.
"""

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, RetryLaterError
from windops_grpc import DomainError, acting_as_service
from windops_migrations import across_tenants, in_tenant

from windops_investigation import staleness
from windops_investigation.cases import mark_stale
from windops_investigation.evidence import as_dict
from windops_investigation.store import (
    ACTIVE,
    CASE_PROPOSALS,
    CASES,
    EVIDENCE_ITEMS,
    EVIDENCE_SNAPSHOTS,
    SERVICE_NAME,
)

ALARM_SUBJECT, EVIDENCE_SUBJECT, SUBMITTED_SUBJECT = (
    "alarm.changed", "evidence.changed", "workorder.submitted",
)  # fmt: skip
RULES = f"service:{SERVICE_NAME}"
logger = logging.getLogger("windops.service")


class Owners(Protocol):
    """Asked as this service, in the event's tenant."""

    async def turbine(self, turbine_id: str) -> Any: ...
    async def parts(self, turbine: Any, lines: list[tuple[str, int]]) -> list[Any]: ...


def parse_parts(text: str) -> list[tuple[str, int, str]]:
    """(part number, quantity, status at submission), from "<number>:<quantity>=<status>,..."."""
    found = []
    for entry in filter(None, text.split(",")):
        head, _, status = entry.partition("=")
        number, _, quantity = head.rpartition(":")
        found.append((number, int(quantity or 1), status))
    return found


def format_parts(parts: Iterable[Any]) -> str:
    return ",".join(f"{p.part_number}:{p.quantity}={p.status}" for p in parts)


async def _fresh_cases(connection: AsyncConnection, tenant: str, **where: str) -> list[Any]:
    """Active cases with frozen, still fresh evidence."""
    c = CASES.c
    query = select(CASES).where(c.tenant_id == tenant, c.status.in_(ACTIVE),
                                c.evidence_snapshot_id != "", c.stale_rule == "")  # fmt: skip
    for name, value in where.items():
        query = query.where(getattr(c, name) == value)
    return list((await connection.execute(query)).all())


async def _frozen(connection: AsyncConnection, tenant: str, snapshot_id: str,
                  record_type: str) -> list[Any]:  # fmt: skip
    i = EVIDENCE_ITEMS.c
    return list((await connection.execute(
        select(i.record_id, i.version, i.content).where(
            i.tenant_id == tenant, i.snapshot_id == snapshot_id, i.record_type == record_type)
    )).all())  # fmt: skip


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _frozen_at(connection: AsyncConnection, tenant: str, snapshot_id: str) -> datetime:
    s = EVIDENCE_SNAPSHOTS.c
    found = (await connection.execute(select(s.created_at).where(
        s.tenant_id == tenant, s.snapshot_id == snapshot_id))).scalar_one()  # fmt: skip
    return _utc(found)


async def _proposals(connection: AsyncConnection, tenant: str, case: Any) -> list[Any]:
    p = CASE_PROPOSALS.c
    return list((await connection.execute(
        select(CASE_PROPOSALS).where(p.tenant_id == tenant, p.case_id == case.case_id,
                                     p.evidence_snapshot_id == case.evidence_snapshot_id)
    )).all())  # fmt: skip


async def on_submitted(engine: AsyncEngine, event: Event) -> bool:
    """Watch a submitted proposal's parts and procedures. False for an older version."""
    if event.producer != "maintenance" or not isinstance(
        event.payload, events_pb2.WorkOrderSubmitted
    ):
        raise InvalidEventError(f"{SUBMITTED_SUBJECT} must come from maintenance")
    s = event.payload
    if not s.work_order_id or not s.case_id or not s.proposal_version.isdigit():
        raise InvalidEventError("A submission needs its work order, case and version")
    version, p = int(s.proposal_version), CASE_PROPOSALS.c
    values = {"version": version, "case_id": s.case_id,
              "evidence_snapshot_id": s.evidence_snapshot_id, "parts": format_parts(s.parts),
              "procedures": ",".join(s.procedures), "submitted_at": event.occurred_at,
              "policy_result_id": s.policy_result_id,
              "impact_result_id": s.impact_result_id}  # fmt: skip
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, event.tenant_id)
            mine = (p.tenant_id == event.tenant_id, p.work_order_id == s.work_order_id)
            known = (await connection.execute(select(p.version).where(*mine))).scalar()
            if known is not None and known >= version:
                return False
            if known is None:
                await connection.execute(
                    CASE_PROPOSALS.insert().values(
                        tenant_id=event.tenant_id, work_order_id=s.work_order_id, **values
                    )
                )
            else:
                await connection.execute(CASE_PROPOSALS.update().where(*mine).values(**values))
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return True


async def on_alarm(engine: AsyncEngine, event: Event) -> int:
    """The alarm rule, for each fresh case on the alarm's turbine. Returns how many went stale."""
    if event.producer != "alarm" or not isinstance(event.payload, events_pb2.AlarmChanged):
        raise InvalidEventError(f"{ALARM_SUBJECT} must come from alarm")
    changed, now, marked = event.payload, datetime.now(UTC), 0
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, event.tenant_id)
            for case in await _fresh_cases(connection, event.tenant_id,
                                           turbine_id=changed.turbine_id):  # fmt: skip
                frozen = {r.record_id: r.version for r in await _frozen(
                    connection, event.tenant_id, case.evidence_snapshot_id, "alarm")}  # fmt: skip
                frozen_at = await _frozen_at(connection, event.tenant_id,
                                             case.evidence_snapshot_id)  # fmt: skip
                found = staleness.alarm(changed, frozen, _utc(event.occurred_at), frozen_at)
                if found is not None:
                    await mark_stale(connection, case, found, now=now, by=RULES)
                    marked += 1
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return marked


async def on_evidence(engine: AsyncEngine, event: Event, owners: Owners, secret: str) -> int:
    """The asset, procedure and inventory rules. Returns how many cases went stale."""
    if not isinstance(event.payload, events_pb2.EvidenceChanged):
        raise InvalidEventError(f"{EVIDENCE_SUBJECT} carries EvidenceChanged")
    changed = event.payload
    if event.producer != changed.source_service:
        raise InvalidEventError("Only a record's owner may say it changed")
    tenant, now = event.tenant_id, datetime.now(UTC)
    try:
        with acting_as_service(SERVICE_NAME, tenant, secret):
            if changed.source_service == "asset":
                return await _asset(engine, tenant, changed.record_id, owners, now)
            if changed.source_service == "knowledge":
                return await _procedure(engine, tenant, changed, now)
            if changed.source_service == "inventory":
                return await _inventory(engine, tenant, changed.record_id, owners, now)
    except DomainError as error:
        # An owner cannot be asked: judge it later, never without it.
        raise RetryLaterError(type(error).__name__) from error
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return 0


async def _asset(engine: AsyncEngine, tenant: str, turbine_id: str, owners: Owners,
                 now: datetime) -> int:  # fmt: skip
    async with engine.connect() as connection:
        await in_tenant(connection, tenant)
        cases = await _fresh_cases(connection, tenant, turbine_id=turbine_id)
    if not cases:
        return 0
    current = as_dict(await owners.turbine(turbine_id))
    marked = 0
    async with engine.begin() as connection:
        await in_tenant(connection, tenant)
        for case in cases:
            [frozen, *_] = await _frozen(connection, tenant, case.evidence_snapshot_id,
                                         "turbine") or [None]  # fmt: skip
            if frozen is None:
                continue
            found = staleness.asset(json.loads(frozen.content), current)
            if found is not None:
                await mark_stale(connection, await _reload(connection, case), found, now=now,
                                 by=RULES)  # fmt: skip
                marked += 1
    return marked


async def _procedure(engine: AsyncEngine, tenant: str, changed: Any, now: datetime) -> int:
    marked = 0
    async with engine.begin() as connection:
        await in_tenant(connection, tenant)
        for case in await _fresh_cases(connection, tenant):
            found = staleness.procedure(changed.record_id, changed.version,
                                        [_Proposal(p) for p in
                                         await _proposals(connection, tenant, case)])  # fmt: skip
            if found is not None:
                await mark_stale(connection, case, found, now=now, by=RULES)
                marked += 1
    return marked


async def _inventory(engine: AsyncEngine, tenant: str, record_id: str, owners: Owners,
                     now: datetime) -> int:  # fmt: skip
    kind, _, rest = record_id.partition(":")
    part, _, farm = rest.partition("@")
    if kind not in ("part", "stock") or not part:
        return 0
    async with engine.connect() as connection:
        await in_tenant(connection, tenant)
        watched = []
        for case in await _fresh_cases(connection, tenant):
            for proposal in await _proposals(connection, tenant, case):
                lines = parse_parts(proposal.parts)
                if any(number == part for number, _, _ in lines):
                    watched.append((case, proposal, lines))
    marked = 0
    for case, proposal, lines in watched:
        turbine = await owners.turbine(case.turbine_id)
        if farm and farm != turbine.wind_farm_id:
            continue
        checks = await owners.parts(turbine, [(n, q) for n, q, _ in lines])
        found = staleness.inventory({n: s for n, _, s in lines},
                                    {c.part_number: c.status for c in checks},
                                    proposal.work_order_id)  # fmt: skip
        if found is None:
            continue
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            await mark_stale(connection, await _reload(connection, case), found, now=now,
                             by=RULES)  # fmt: skip
            marked += 1
    return marked


async def sweep(engine: AsyncEngine, now: datetime | None = None) -> int:
    """The age rule, across every tenant. Returns how many cases went stale."""
    now = now or datetime.now(UTC)
    c, s = CASES.c, EVIDENCE_SNAPSHOTS.c
    async with engine.connect() as connection:
        await across_tenants(connection)
        rows = (await connection.execute(
            select(c.tenant_id, c.case_id, c.severity, c.evidence_snapshot_id, s.created_at)
            .join(EVIDENCE_SNAPSHOTS, (s.tenant_id == c.tenant_id)
                  & (s.snapshot_id == c.evidence_snapshot_id))
            .where(c.status.in_(ACTIVE), c.stale_rule == "")
        )).all()  # fmt: skip
    marked = 0
    for row in rows:
        frozen_at = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
        found = staleness.age(row.severity, frozen_at, now)
        if found is None:
            continue
        async with engine.begin() as connection:
            await in_tenant(connection, row.tenant_id)
            case = (await connection.execute(select(CASES).where(
                c.tenant_id == row.tenant_id, c.case_id == row.case_id))).one()  # fmt: skip
            if case.evidence_snapshot_id != row.evidence_snapshot_id:
                continue  # Re-evaluated meanwhile: the new snapshot is judged next time.
            await mark_stale(connection, case, found, now=now, by=RULES)
            marked += 1
    return marked


async def keep_sweeping(engine: AsyncEngine, stop: Any, every_seconds: float = 300.0) -> None:
    """The age rule, every few minutes. A failed pass is tried again on the next."""
    import asyncio
    import contextlib

    while not stop.is_set():
        try:
            marked = await sweep(engine)
            if marked:
                logger.info("Evidence went stale with age", extra={"cases": marked})
        except Exception as error:  # Tried again on the next pass.
            logger.warning("Could not sweep for stale evidence",
                           extra={"error_type": type(error).__name__})  # fmt: skip
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)


async def _reload(connection: AsyncConnection, case: Any) -> Any:
    c = CASES.c
    return (
        await connection.execute(
            select(CASES).where(c.tenant_id == case.tenant_id, c.case_id == case.case_id)
        )
    ).one()


class _Proposal:
    """A watched proposal as the rules read it."""

    def __init__(self, row: Any) -> None:
        self.work_order_id = row.work_order_id
        self.procedures = [p for p in row.procedures.split(",") if p]
