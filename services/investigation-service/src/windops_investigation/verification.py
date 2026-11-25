"""Step 15: verify the work once it is done (SRS 5.4, FR-MNT-005, M10.6, ADR 0038).

    workorder.completed ──> waiting, the case verifying ──window passed──> reported, the case
        │                                                                   verified ──a person──>
        └──the case no longer approved on that snapshot──> not_started        closed, or reopened

**Who says the work is done:** a person with maintenance.complete, through the Maintenance
Service, which announces `workorder.completed` (FR-MNT-004). This service owns the case, so it
verifies the work.

**The comparison**, worked out, never judged by a model:
- **before:** the telemetry frozen in the evidence the approved version stood on;
- **after:** the turbine's telemetry over the window from the work's completion, frozen as a
  telemetry snapshot of its own, with its hash, once the window has passed;
- each metric's mean and maximum, before and after, for a person to read.

**The outcome** comes from the fault rules, run again by the Fault Detection Service at the end of
the window. Each rule judges the window's end, and the recent part each looks at, ten minutes at
most, fits inside the window:
- **inconclusive:** the window holds fewer than MINIMUM_SAMPLES samples, or a rule that counts
  finds a data gap;
- **not_resolved:** a rule that counts finds anything else;
- **resolved:** none does.

The rules that count are the case's own, from its detections, or any rule for a case a person
opened. Then the case is `verified` until a person closes it, or reopens it to be investigated
again: the outcome is reported, and nothing is closed on it alone.

**When an owner is down:** the verification is tried again, sooner then later, up to every five
minutes (SRS 7.7). One still not done a day after its window is reported inconclusive, saying
why.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops.telemetry.v1 import telemetry_pb2
from windops_events import Event, InvalidEventError, RetryLaterError
from windops_grpc import DomainError, acting_as_service, service_subject
from windops_migrations import across_tenants, in_tenant

from windops_investigation import cases
from windops_investigation.progress import add_progress
from windops_investigation.store import (
    APPROVED,
    CASE_DETECTIONS,
    CASES,
    EVIDENCE_ITEMS,
    OPEN,
    SERVICE_NAME,
    VERIFICATIONS,
    VERIFIED,
    VERIFYING,
)

logger = logging.getLogger("windops.service")
SUBJECT = "workorder.completed"
# Only the Maintenance Service may say work was completed.
PRODUCER = "maintenance"
VERIFIER = service_subject(SERVICE_NAME)
WAITING, REPORTED, NOT_STARTED = "waiting", "reported", "not_started"
RESOLVED, NOT_RESOLVED, INCONCLUSIVE = "resolved", "not_resolved", "inconclusive"
OUTCOMES = (RESOLVED, NOT_RESOLVED, INCONCLUSIVE)
DEFAULT_WINDOW = timedelta(hours=1)
WINDOW_SETTING = "WINDOPS_VERIFICATION_WINDOW_MINUTES"
# The Telemetry Service freezes at most a day.
MAXIMUM_WINDOW = timedelta(hours=24)
# Fewer samples than this after the work say too little.
MINIMUM_SAMPLES = 10
# Samples reach storage within seconds, so the window is frozen a little after it ends.
SETTLE = timedelta(seconds=30)
FIRST_RETRY, LONGEST_RETRY = timedelta(seconds=15), timedelta(minutes=5)
# One still not verified this long after its window is reported inconclusive.
GIVE_UP = timedelta(days=1)
DATA_GAP = "data_gap"
CLOSE, REOPEN = "close", "reopen"
REVIEWS = {CLOSE: "closed", REOPEN: "reopened"}


def window_setting(environ: Mapping[str, str] | None = None) -> timedelta:
    """The window, from WINDOPS_VERIFICATION_WINDOW_MINUTES: an hour unless set, at most a day."""
    raw = (environ if environ is not None else os.environ).get(WINDOW_SETTING, "")
    if not raw:
        return DEFAULT_WINDOW
    if not raw.isdigit() or not 1 <= int(raw) <= MAXIMUM_WINDOW.total_seconds() / 60:
        raise SystemExit(f"{WINDOW_SETTING} is whole minutes, from 1 to 1440")
    return timedelta(minutes=int(raw))


class Sources(Protocol):
    """The telemetry and the fault rules, asked as this service."""

    async def telemetry_snapshot(
        self, turbine_id: str, start: datetime, end: datetime, key: str
    ) -> Any: ...
    async def summarize(self, snapshot_id: str) -> list[Any]: ...
    async def evaluate(self, turbine_id: str, at: datetime) -> Any: ...


@dataclass(frozen=True)
class Finding:
    rule_id: str
    version: int
    kind: str
    severity: str
    summary: str


def judge(
    after_samples: int, findings: Iterable[Finding], case_rules: set[str]
) -> tuple[str, list[dict[str, Any]], str]:
    """(outcome, each rule's result, why). The case's own rules count, or every rule when it has
    none."""
    found = list(findings)
    counted = [f for f in found if not case_rules or f.rule_id in case_rules]
    results = [
        {"rule_id": f.rule_id, "version": f.version, "severity": f.severity,
         "summary": f.summary[:500], "counts": f in counted}
        for f in found
    ]  # fmt: skip
    kinds = [f.kind for f in counted]
    if after_samples < MINIMUM_SAMPLES:
        return INCONCLUSIVE, results, f"Only {after_samples} samples after the work"
    if DATA_GAP in kinds:
        return INCONCLUSIVE, results, "A rule that counts found a data gap after the work"
    if kinds:
        fired = sorted({f.rule_id for f in counted})
        return NOT_RESOLVED, results, f"Still found after the work: {', '.join(fired)}"
    return RESOLVED, results, "None of the case's rules finds anything after the work"


def _figure(summary: Any, name: str) -> float | None:
    return round(float(getattr(summary, name)), 4) if summary.HasField(name) else None


def compare(before: Sequence[Any], after: Sequence[Any]) -> list[dict[str, Any]]:
    """Each metric's mean and maximum before and after, in the Telemetry Service's order."""
    earlier = {s.metric: s for s in before}
    later = {s.metric: s for s in after}
    compared = []
    for metric in sorted(set(earlier) | set(later)):
        b, a = earlier.get(metric), later.get(metric)
        either: Any = a or b
        compared.append({
            "metric": telemetry_pb2.Metric.Name(metric), "unit": either.unit,
            "before_mean": _figure(b, "mean") if b else None,
            "after_mean": _figure(a, "mean") if a else None,
            "before_maximum": _figure(b, "maximum") if b else None,
            "after_maximum": _figure(a, "maximum") if a else None,
            "before_readings": int(b.good + b.suspect) if b else 0,
            "after_readings": int(a.good + a.suspect) if a else 0,
        })  # fmt: skip
    return compared


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _locked(connection: AsyncConnection, tenant: str, case_id: str) -> Any | None:
    await in_tenant(connection, tenant)
    return (await connection.execute(
        select(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
        .with_for_update()
    )).one_or_none()  # fmt: skip


async def on_completed(engine: AsyncEngine, event: Event, window: timedelta) -> bool:
    """Keep one completion, to be verified once its window passes. False for a repeat, or work
    for a case this service does not have."""
    if event.producer != PRODUCER or not isinstance(event.payload, events_pb2.WorkOrderCompleted):
        raise InvalidEventError(f"{SUBJECT} must come from {PRODUCER}")
    c = event.payload
    if not (
        c.work_order_id
        and c.case_id
        and c.turbine_id
        and c.proposal_version.isdigit()
        and c.completed_by
        and c.HasField("completed_at")
        and c.HasField("started_at")
    ):
        raise InvalidEventError("A completion names its work order, version, case, turbine, "
                                "who completed it and when")  # fmt: skip
    completed_at = c.completed_at.ToDatetime(tzinfo=UTC)
    version, now = int(c.proposal_version), datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            case = await _locked(connection, event.tenant_id, c.case_id)
            if case is None:
                return False
            v = VERIFICATIONS.c
            known = (await connection.execute(select(v.work_order_id).where(
                v.tenant_id == event.tenant_id, v.work_order_id == c.work_order_id,
                v.proposal_version == version))).first()  # fmt: skip
            if known is not None:
                return False
            waits = case.status == APPROVED and case.evidence_snapshot_id == c.evidence_snapshot_id
            note = "" if waits else (
                f"The case is {case.status}, on evidence {case.evidence_snapshot_id or 'none'}, "
                "not approved on the evidence this work stood on")  # fmt: skip
            await connection.execute(VERIFICATIONS.insert().values(
                tenant_id=event.tenant_id, work_order_id=c.work_order_id,
                proposal_version=version, case_id=c.case_id, turbine_id=c.turbine_id,
                evidence_snapshot_id=c.evidence_snapshot_id, completed_by=c.completed_by,
                started_at=c.started_at.ToDatetime(tzinfo=UTC), completed_at=completed_at,
                status=WAITING if waits else NOT_STARTED, window_start=completed_at,
                window_end=completed_at + window, next_attempt_at=completed_at + window + SETTLE,
                note=note, received_at=now, updated_at=now,
            ))  # fmt: skip
            reference = f"work order {c.work_order_id} v{version}"
            await add_progress(
                connection, case, key=f"completed:{c.work_order_id}:{version}", kind="completed",
                at=now, status=WAITING if waits else NOT_STARTED, note=reference,
            )  # fmt: skip
            if waits:
                await cases.change(
                    connection, case, now=now, action="maintenance_completed",
                    actor_type=cases.USER, actor_id=c.completed_by,
                    note=f"{reference}: verified after {window}", status=VERIFYING,
                    work_order_id=c.work_order_id,
                )  # fmt: skip
    except IntegrityError as error:
        raise RetryLaterError("A concurrent change won") from error
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return True


@dataclass(frozen=True)
class Report:
    outcome: str
    note: str
    after_snapshot_id: str = ""
    after_samples: int = 0
    metrics: Sequence[Mapping[str, Any]] = ()
    rules: Sequence[Mapping[str, Any]] = ()
    before_snapshot_id: str = ""


class Verifier:
    """Verifies each completion whose window has passed, as this service, across tenants."""

    def __init__(self, engine: AsyncEngine, sources: Sources, secret: str) -> None:
        self.engine, self.sources, self.secret = engine, sources, secret

    async def run_once(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        v = VERIFICATIONS.c
        async with self.engine.connect() as connection:
            await across_tenants(connection)
            due = (await connection.execute(
                select(VERIFICATIONS).where(v.status == WAITING, v.next_attempt_at <= now)
                .order_by(v.next_attempt_at)
            )).all()  # fmt: skip
        reported = 0
        for row in due:
            try:
                with acting_as_service(SERVICE_NAME, row.tenant_id, self.secret):
                    report = await self.measure(row)
            except DomainError as error:
                if now - _utc(row.window_end) < GIVE_UP:
                    await self._later(row, now, type(error).__name__)
                    continue
                report = Report(INCONCLUSIVE, f"It could not be checked within a day: "
                                              f"{type(error).__name__}")  # fmt: skip
            if await self._report(row, report, now):
                reported += 1
        return reported

    async def measure(self, row: Any) -> Report:
        """Compare the window with the evidence, and run the rules at its end."""
        async with self.engine.connect() as connection:
            await in_tenant(connection, row.tenant_id)
            i, d = EVIDENCE_ITEMS.c, CASE_DETECTIONS.c
            before_id = (await connection.execute(select(i.record_id).where(
                i.tenant_id == row.tenant_id, i.snapshot_id == row.evidence_snapshot_id,
                i.record_type == "telemetry_snapshot"))).scalar() or ""  # fmt: skip
            case_rules = set((await connection.execute(select(d.rule_id).where(
                d.tenant_id == row.tenant_id, d.case_id == row.case_id))).scalars())  # fmt: skip
        start, end = _utc(row.window_start), _utc(row.window_end)
        key = f"verify-{row.work_order_id}-v{row.proposal_version}"
        after = await self.sources.telemetry_snapshot(row.turbine_id, start, end, key)
        later = await self.sources.summarize(after.snapshot_id)
        earlier = await self.sources.summarize(before_id) if before_id else []
        evaluated = await self.sources.evaluate(row.turbine_id, end)
        findings = [Finding(f.rule_id, f.version, f.kind, f.severity, f.summary)
                    for f in evaluated.findings]  # fmt: skip
        outcome, rules, why = judge(int(after.sample_count), findings, case_rules)
        return Report(outcome, why, after.snapshot_id, int(after.sample_count),
                      compare(earlier, later), rules, before_id)  # fmt: skip

    async def _later(self, row: Any, now: datetime, why: str) -> None:
        wait = min(FIRST_RETRY * 2 ** min(int(row.attempts), 10), LONGEST_RETRY)
        logger.warning("Could not verify completed work yet",
                       extra={"work_order_id": row.work_order_id, "error_type": why})  # fmt: skip
        async with self.engine.begin() as connection:
            await in_tenant(connection, row.tenant_id)
            await connection.execute(update(VERIFICATIONS).where(*_mine(row)).values(
                attempts=row.attempts + 1, next_attempt_at=now + wait, updated_at=now,
                note=f"Tried again later: {why}"))  # fmt: skip

    async def _report(self, row: Any, report: Report, now: datetime) -> bool:
        """Keep the report, and move the case if it still waits on this work."""
        async with self.engine.begin() as connection:
            case = await _locked(connection, row.tenant_id, row.case_id)
            done = await connection.execute(
                update(VERIFICATIONS).where(*_mine(row), VERIFICATIONS.c.status == WAITING).values(
                    status=REPORTED, outcome=report.outcome, note=report.note[:1000],
                    before_telemetry_snapshot_id=report.before_snapshot_id,
                    after_telemetry_snapshot_id=report.after_snapshot_id,
                    after_samples=report.after_samples,
                    metrics_json=json.dumps(list(report.metrics), sort_keys=True),
                    rules_json=json.dumps(list(report.rules), sort_keys=True),
                    attempts=row.attempts + 1, reported_at=now, updated_at=now,
                )
            )  # fmt: skip
            if done.rowcount != 1 or case is None:
                return False
            reference = f"work order {row.work_order_id} v{row.proposal_version}"
            await add_progress(
                connection, case, key=f"verified:{row.work_order_id}:{row.proposal_version}",
                kind="verified", at=now, status=report.outcome, note=reference,
            )  # fmt: skip
            if case.status == VERIFYING:
                await cases.change(
                    connection, case, now=now, action="verification_reported",
                    actor_type=cases.SERVICE, actor_id=VERIFIER,
                    note=f"{reference}: {report.outcome}. {report.note}"[:1000], status=VERIFIED,
                    work_order_id=row.work_order_id, outcome=report.outcome,
                )  # fmt: skip
        logger.info("Verified completed work", extra={"work_order_id": row.work_order_id,
                                                      "outcome": report.outcome})  # fmt: skip
        return True


def _mine(row: Any) -> tuple[Any, ...]:
    v = VERIFICATIONS.c
    return (v.tenant_id == row.tenant_id, v.work_order_id == row.work_order_id,
            v.proposal_version == row.proposal_version)  # fmt: skip


async def newest(connection: AsyncConnection, tenant: str, case_id: str) -> Any | None:
    v = VERIFICATIONS.c
    return (await connection.execute(
        select(VERIFICATIONS).where(v.tenant_id == tenant, v.case_id == case_id,
                                    v.status == REPORTED)
        .order_by(v.reported_at.desc()).limit(1)
    )).one_or_none()  # fmt: skip


async def of_case(connection: AsyncConnection, tenant: str, case_id: str) -> list[Any]:
    v = VERIFICATIONS.c
    return list((await connection.execute(
        select(VERIFICATIONS).where(v.tenant_id == tenant, v.case_id == case_id)
        .order_by(v.received_at.desc())
    )).all())  # fmt: skip


async def review(
    connection: AsyncConnection, case: Any, verification: Any, *, decision: str, by: str,
    reason: str, now: datetime,
) -> Any:  # fmt: skip
    """A person's answer to the report, under the case's row lock: closed, or open again."""
    await connection.execute(update(VERIFICATIONS).where(*_mine(verification)).values(
        review=REVIEWS[decision], reviewed_by=by, reviewed_at=now, review_reason=reason,
        updated_at=now))  # fmt: skip
    reference = f"work order {verification.work_order_id} v{verification.proposal_version}"
    note = f"{reference}: {verification.outcome}" + (f". {reason}" if reason else "")
    if decision == CLOSE:
        return await cases.change(
            connection, case, now=now, action="closed", actor_type=cases.USER, actor_id=by,
            note=note[:1000], status="closed", work_order_id=verification.work_order_id,
            outcome=verification.outcome,
        )  # fmt: skip
    return await cases.change(
        connection, case, now=now, action="reopened", actor_type=cases.USER, actor_id=by,
        note=note[:1000], status=OPEN, work_order_id=verification.work_order_id,
        outcome=verification.outcome,
    )  # fmt: skip


async def keep_verifying(verifier: Verifier, stop: asyncio.Event,
                         every_seconds: float = 30.0) -> None:  # fmt: skip
    while not stop.is_set():
        try:
            await verifier.run_once()
        except Exception as error:  # Tried again shortly. The completions are kept meanwhile.
            logger.warning("Could not run the verifier",
                           extra={"error_type": type(error).__name__})  # fmt: skip
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)
