"""The detector: each streaming turbine's rules, on a schedule, and a record of what they find.

Telemetry announces each stored batch as telemetry.received, which marks its turbine as streaming.
Every interval the detector evaluates each streaming turbine's enabled rules, acting as the Fault
Detection Service itself, in that turbine's tenant, with its fixed read grants (ADR 0008).

A finding becomes a detection (FR-FLT-003): the exact rule version and its hash, every value the
finding rests on, the alarms it saw, and a telemetry snapshot of the window, frozen by the
Telemetry Service. Then fault.detected is published. A detection stays open while its rule keeps
finding it at the same severity and version, and closes when the rule stops finding it, or finds
it at another severity or under a newer version, which is a detection of its own.

Each turbine's window stays in memory between runs, so a run fetches only what arrived since the
last, with an overlap for late samples. Only a turbine's first run reads its whole window.

If the Telemetry Service cannot freeze a snapshot, no detection is made, and the next run tries
again (SRS 7.7): a detection never stands without the evidence behind it.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops_auth import identity_scope, sign_identity
from windops_events import Event, InvalidEventError
from windops_grpc import DomainError, VerifiedIdentity
from windops_migrations import across_tenants, in_tenant
from windops_observability import tenant_context
from windops_outbox import add_event

from windops_fault_detection import engine, library
from windops_fault_detection.store import DETECTIONS, SERVICE_NAME, TURBINE_WATCH, audit

logger = logging.getLogger("windops.service")
SUBJECT = "telemetry.received"
# Only the Telemetry Service may say a turbine's telemetry arrived.
PRODUCER = "telemetry"
DETECTED_SUBJECT = "fault.detected"
SERVICE_SUBJECT = f"service:{SERVICE_NAME}"
# A turbine whose telemetry stopped this long ago is no longer evaluated.
STREAMING_WITHIN = timedelta(minutes=15)
# Samples reach storage within seconds, so the window ends a little before now.
SETTLE = timedelta(seconds=30)
OPEN, CLOSED = "open", "closed"
# Fetched again each run, so a sample that arrived late still reaches the window.
OVERLAP = timedelta(minutes=2)


@dataclass
class Buffer:
    """One turbine's recent samples, by time, and what was fetched."""

    readings: tuple[str, ...]
    fetched_until: datetime
    samples: dict[datetime, Any] = field(default_factory=dict)


class Sources(Protocol):
    async def turbine(self, turbine_id: str) -> Any: ...
    async def samples(
        self, turbine_id: str, start: datetime, end: datetime, readings: list[str]
    ) -> list[Any]: ...
    async def alarms(self, turbine_id: str) -> list[Any]: ...
    async def snapshot(self, turbine_id: str, start: datetime, end: datetime, key: str) -> Any: ...


def new_detection_id() -> str:
    return f"det_{uuid.uuid4().hex[:20]}"


async def watch(engine_: AsyncEngine, event: Event, now: datetime | None = None) -> None:
    """Mark the batch's turbine as streaming. Safe to repeat (EVT-003)."""
    if event.producer != PRODUCER or not isinstance(event.payload, events_pb2.TelemetryReceived):
        raise InvalidEventError(f"telemetry.received must come from {PRODUCER}")
    turbine = event.payload.turbine_id
    if not turbine:
        raise InvalidEventError("telemetry.received needs a turbine")
    now = now or datetime.now(UTC)
    async with engine_.begin() as connection:
        await in_tenant(connection, event.tenant_id)
        dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
        statement = dialect.insert(TURBINE_WATCH).values(
            tenant_id=event.tenant_id, turbine_id=turbine, last_seen_at=now
        )
        await connection.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "turbine_id"], set_={"last_seen_at": now}
            )
        )


@contextlib.contextmanager
def acting_as_service(tenant: str, secret: str) -> Iterator[None]:
    """Calls made inside go out as the Fault Detection Service itself, in this tenant."""
    identity = VerifiedIdentity(subject=SERVICE_SUBJECT, tenant_id=tenant)
    token = sign_identity(identity, secret, ttl_seconds=120)
    with identity_scope(identity, token), tenant_context(tenant):
        yield


class Detector:
    def __init__(
        self, engine_: AsyncEngine, sources: Sources, secret: str, interval: timedelta,
    ) -> None:  # fmt: skip
        self.engine, self.sources, self.secret, self.interval = engine_, sources, secret, interval
        self.buffers: dict[tuple[str, str], Buffer] = {}

    async def window_samples(
        self, tenant: str, turbine_id: str, start: datetime, end: datetime, readings: list[str]
    ) -> list[Any]:
        """The samples in (start, end], fetching only what the buffer does not hold yet."""
        key, wanted = (tenant, turbine_id), tuple(readings)
        buffer = self.buffers.get(key)
        if buffer is None or buffer.readings != wanted or buffer.fetched_until < start:
            buffer = Buffer(wanted, start)
            fetch_from = start
        else:
            fetch_from = max(start, buffer.fetched_until - OVERLAP)
        for sample in await self.sources.samples(turbine_id, fetch_from, end, readings):
            buffer.samples[sample.at] = sample
        buffer.samples = {at: s for at, s in buffer.samples.items() if start < at <= end}
        buffer.fetched_until = end
        self.buffers[key] = buffer
        return [buffer.samples[at] for at in sorted(buffer.samples)]

    async def due(self, now: datetime) -> list[tuple[str, str]]:
        w = TURBINE_WATCH.c
        async with self.engine.connect() as connection:
            await across_tenants(connection)
            rows = (
                await connection.execute(
                    select(w.tenant_id, w.turbine_id)
                    .where(
                        w.last_seen_at >= now - STREAMING_WITHIN,
                        or_(w.last_evaluated_at.is_(None),
                            w.last_evaluated_at <= now - self.interval),
                    )
                    .order_by(w.tenant_id, w.turbine_id)
                )
            ).all()  # fmt: skip
        return [(row.tenant_id, row.turbine_id) for row in rows]

    async def run_once(self, now: datetime | None = None) -> int:
        """Evaluate every streaming turbine that is due. Returns how many were evaluated."""
        now = now or datetime.now(UTC)
        due = await self.due(now)
        for tenant, turbine in due:
            try:
                with acting_as_service(tenant, self.secret):
                    await self.evaluate(tenant, turbine, now)
            except DomainError as error:
                # An owner could not be reached, or the turbine is gone: tried again next time.
                logger.warning(
                    "Could not evaluate a turbine",
                    extra={"turbine_id": turbine, "error_type": type(error).__name__},
                )
            async with self.engine.begin() as connection:
                await in_tenant(connection, tenant)
                await connection.execute(
                    update(TURBINE_WATCH)
                    .where(TURBINE_WATCH.c.tenant_id == tenant,
                           TURBINE_WATCH.c.turbine_id == turbine)
                    .values(last_evaluated_at=now)
                )  # fmt: skip
        return len(due)

    async def evaluate(self, tenant: str, turbine_id: str, now: datetime) -> None:
        await library.install_defaults(self.engine, tenant, now)
        async with self.engine.connect() as connection:
            await in_tenant(connection, tenant)
            loaded = await library.enabled_rules(connection, tenant)
        rules = [rule for rule, _ in loaded]
        hashes = {rule.rule_id: content for rule, content in loaded}
        end = now - SETTLE
        start = end - engine.lookback(rules)
        turbine = await self.sources.turbine(turbine_id)
        samples = await self.window_samples(
            tenant, turbine_id, start, end, engine.readings_needed(rules)
        )
        alarms = [a for a in await self.sources.alarms(turbine_id) if a.raised_at <= end]
        window = engine.Window(
            turbine_id, turbine.model.rated_power_kw, turbine.model.rotor_diameter_m, end,
            samples, alarms,
        )  # fmt: skip
        await self.record(tenant, turbine_id, engine.evaluate(rules, window), hashes, start, end,
                          now)  # fmt: skip

    async def record(
        self, tenant: str, turbine_id: str, findings: list[engine.Finding],
        hashes: dict[str, str], start: datetime, end: datetime, now: datetime,
    ) -> list[str]:  # fmt: skip
        """Open, keep or close each detection to match what the rules found. Returns new IDs."""
        d = DETECTIONS.c
        async with self.engine.connect() as connection:
            await in_tenant(connection, tenant)
            standing = (
                await connection.execute(
                    select(DETECTIONS).where(
                        d.tenant_id == tenant, d.turbine_id == turbine_id, d.state == OPEN
                    )
                )
            ).all()
        open_by_rule = {row.rule_id: row for row in standing}
        found = {finding.rule_id: finding for finding in findings}
        keep, new = [], []
        for finding in findings:
            row = open_by_rule.get(finding.rule_id)
            if row is not None and (row.rule_version, row.severity) == (
                finding.version, finding.severity,
            ):  # fmt: skip
                keep.append((row, finding))
            else:
                new.append(finding)
        # Freeze the evidence first. Without it there is no detection, and the old one stands.
        frozen = []
        for finding in new:
            detection_id = new_detection_id()
            try:
                snapshot = await self.sources.snapshot(turbine_id, start, end, detection_id)
            except DomainError as error:
                logger.warning(
                    "Could not freeze telemetry for a detection",
                    extra={"turbine_id": turbine_id, "rule_id": finding.rule_id,
                           "error_type": type(error).__name__},
                )  # fmt: skip
                continue
            frozen.append((detection_id, finding, snapshot))
        replaced = {finding.rule_id for _, finding, _ in frozen}
        async with self.engine.begin() as connection:
            await in_tenant(connection, tenant)
            for row, finding in keep:
                await connection.execute(
                    update(DETECTIONS)
                    .where(d.tenant_id == tenant, d.detection_id == row.detection_id)
                    .values(last_seen_at=now, amount=finding.amount)
                )
            for row in standing:
                reason = "replaced" if row.rule_id in replaced else (
                    "cleared" if row.rule_id not in found else ""
                )  # fmt: skip
                if reason:
                    await connection.execute(
                        update(DETECTIONS)
                        .where(d.tenant_id == tenant, d.detection_id == row.detection_id)
                        .values(state=CLOSED, closed_at=now, closed_reason=reason)
                    )
            for detection_id, finding, snapshot in frozen:
                await self._open(connection, tenant, turbine_id, detection_id, finding,
                                 hashes[finding.rule_id], snapshot, start, end, now)  # fmt: skip
        return [detection_id for detection_id, _, _ in frozen]

    @staticmethod
    async def _open(
        connection: AsyncConnection, tenant: str, turbine_id: str, detection_id: str,
        finding: engine.Finding, rule_hash: str, snapshot: Any, start: datetime, end: datetime,
        now: datetime,
    ) -> None:  # fmt: skip
        observed = dict(finding.observed)
        await connection.execute(
            DETECTIONS.insert().values(
                tenant_id=tenant, detection_id=detection_id, turbine_id=turbine_id,
                rule_id=finding.rule_id, rule_version=finding.version,
                rule_content_hash=rule_hash, kind=finding.kind, severity=finding.severity,
                amount=finding.amount, summary=finding.summary,
                observed=json.dumps(observed, sort_keys=True), since=finding.since,
                detected_at=now, last_seen_at=now, window_start=start, window_end=end,
                telemetry_snapshot_id=snapshot.snapshot_id,
                telemetry_snapshot_hash=snapshot.content_hash,
                alarm_ids=json.dumps(observed.get("alarm_ids", [])), state=OPEN,
            )
        )  # fmt: skip
        await add_event(
            connection,
            DETECTED_SUBJECT,
            tenant_id=tenant,
            payload=events_pb2.FaultDetected(
                detection_id=detection_id, turbine_id=turbine_id,
                rule_version=str(finding.version), telemetry_snapshot_id=snapshot.snapshot_id,
                rule_id=finding.rule_id, severity=finding.severity, rule_content_hash=rule_hash,
            ),
            producer=SERVICE_NAME,
        )  # fmt: skip
        await audit(connection, tenant_id=tenant, actor_type="service", actor_id=SERVICE_SUBJECT,
                    action="fault.detected", rule_id=detection_id,
                    resource_type="detection")  # fmt: skip


async def keep_detecting(detector: Detector, stop: Any, every_seconds: float = 30.0) -> None:
    """Look for due turbines every half minute; each is evaluated once an interval."""
    while not stop.is_set():
        try:
            done = await detector.run_once()
            if done:
                logger.info("Evaluated turbines", extra={"turbines": done})
        except Exception as error:  # Tried again shortly. Telemetry is safe meanwhile.
            logger.warning("Could not run the detector", extra={"error_type": type(error).__name__})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)
