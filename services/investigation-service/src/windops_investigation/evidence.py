"""Evidence snapshots: a case's evidence, frozen and hashed (FR-INV-002, EVD-001 to 004).

A snapshot holds, each as it was when frozen:

- the turbine, from the Asset Service, with its version;
- the case's telemetry window, which the Telemetry Service freezes as a telemetry snapshot of its
  own, with a content hash and a retention hold (EVD-003), and its data quality;
- every alarm on the turbine in the window, from the Alarm Service, with its version;
- each of the case's detections, and the exact fault rule version behind it, from the Fault
  Detection Service.

Each item is canonical JSON, with its own sha256. The snapshot's hash is sha256 over a header and
every item's identity and hash, written as HASH_FORMAT says, so anyone holding the items can check
it. A database trigger refuses to change or delete a snapshot once written.

If any source cannot be reached, there is no snapshot, and the case's evidence stays pending until
the next try (SRS 7.7): a snapshot is never partial.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from google.protobuf.json_format import MessageToDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_grpc import DomainError, acting_as_service, service_subject
from windops_migrations import across_tenants, in_tenant

from windops_investigation.store import (
    ACTIVE,
    CASE_DETECTIONS,
    CASES,
    EVIDENCE_ITEMS,
    EVIDENCE_SNAPSHOTS,
    SERVICE_NAME,
    record,
)

logger = logging.getLogger("windops.service")
HASH_FORMAT = "windops-evidence-snapshot-v1"
BUILDER = service_subject(SERVICE_NAME)
# A case with no detections looks at its turbine's last two hours.
DEFAULT_WINDOW = timedelta(hours=2)
# The Telemetry Service freezes at most a day.
MAXIMUM_WINDOW = timedelta(hours=24)
# Samples reach storage within seconds, so the window ends a little before now.
SETTLE = timedelta(seconds=30)


class Sources(Protocol):
    """Each owner's records, asked as whoever is freezing: a person, or this service itself."""

    async def turbine(self, turbine_id: str) -> Any: ...
    async def telemetry_snapshot(
        self, turbine_id: str, start: datetime, end: datetime, key: str
    ) -> Any: ...
    async def alarms(self, turbine_id: str) -> list[Any]: ...
    async def detection(self, detection_id: str) -> Any: ...
    async def rule_version(self, rule_id: str, version: int) -> Any: ...


@dataclass(frozen=True)
class Item:
    source_service: str
    record_type: str
    record_id: str
    version: str
    content: Mapping[str, Any]
    window_start: datetime | None = None
    window_end: datetime | None = None
    data_quality: Mapping[str, Any] | None = None

    @property
    def content_json(self) -> str:
        return canonical(self.content)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content_json.encode()).hexdigest()


def canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def as_dict(message: Any) -> dict[str, Any]:
    return MessageToDict(message, preserving_proto_field_name=True)


def snapshot_hash(
    tenant: str, case_id: str, version: int, start: datetime, end: datetime,
    items: Sequence[tuple[str, str, str, str, str]],
) -> str:  # fmt: skip
    """Over "<format>|<tenant>|<case>|<version>|<from>|<to>", then a line an item: its source,
    type, ID, version and content hash, "|"-separated, in the snapshot's order."""
    lines = [f"{HASH_FORMAT}|{tenant}|{case_id}|{version}|{start.isoformat()}|{end.isoformat()}"]
    lines += ["|".join(item) for item in items]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def new_snapshot_id() -> str:
    return f"evs_{uuid.uuid4().hex[:20]}"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _stamp(message: Any, name: str) -> datetime | None:
    return message.__getattribute__(name).ToDatetime(tzinfo=UTC) if message.HasField(name) else None


async def gather(
    sources: Sources, turbine_id: str, detection_ids: Sequence[str], snapshot_id: str,
    now: datetime,
) -> tuple[datetime, datetime, list[Item]]:  # fmt: skip
    """Every item a snapshot holds, read from its owner now. Raises if any cannot be read."""
    detections = [await sources.detection(d) for d in detection_ids]
    end = now - SETTLE
    starts = [s for d in detections if (s := _stamp(d, "window_start")) is not None]
    start = max(min(starts, default=end - DEFAULT_WINDOW), end - MAXIMUM_WINDOW)
    turbine = await sources.turbine(turbine_id)
    items = [Item("asset", "turbine", turbine_id, str(turbine.version), as_dict(turbine))]
    telemetry = await sources.telemetry_snapshot(turbine_id, start, end, snapshot_id)
    manifest = telemetry.manifest
    items.append(Item(
        "telemetry", "telemetry_snapshot", telemetry.snapshot_id, telemetry.content_hash,
        as_dict(telemetry), start, end,
        {"quality": {q.label: q.samples for q in manifest.quality},
         "gaps": sum(g.missing for g in manifest.gaps), "rejected": manifest.rejected,
         "samples": telemetry.sample_count},
    ))  # fmt: skip
    for alarm in await sources.alarms(turbine_id):
        raised, cleared = _stamp(alarm, "raised_at"), _stamp(alarm, "cleared_at")
        if raised is not None and raised <= end and (cleared is None or cleared >= start):
            items.append(Item("alarm", "alarm", alarm.alarm_id, str(alarm.version),
                              as_dict(alarm)))  # fmt: skip
    for detection in detections:
        items.append(Item("fault-detection", "detection", detection.detection_id,
                          str(detection.rule_version), as_dict(detection)))  # fmt: skip
    rules = sorted({(d.rule_id, d.rule_version) for d in detections})
    for rule_id, version in rules:
        rule = await sources.rule_version(rule_id, version)
        items.append(Item("fault-detection", "fault_rule_version", f"{rule_id}@{version}",
                          str(version), as_dict(rule)))  # fmt: skip
    return start, end, items


async def freeze(
    engine: AsyncEngine, sources: Sources, tenant: str, case_id: str, by: str, actor_type: str,
    now: datetime | None = None,
) -> str:  # fmt: skip
    """Freeze a new version of the case's evidence. Returns its ID. Raises if a source cannot be
    read, leaving the case's evidence as it was."""
    now = now or datetime.now(UTC)
    async with engine.connect() as connection:
        await in_tenant(connection, tenant)
        case = (await connection.execute(
            select(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
        )).one()  # fmt: skip
        detection_ids = list((await connection.execute(
            select(CASE_DETECTIONS.c.detection_id)
            .where(CASE_DETECTIONS.c.tenant_id == tenant, CASE_DETECTIONS.c.case_id == case_id)
            .order_by(CASE_DETECTIONS.c.attached_at, CASE_DETECTIONS.c.detection_id)
        )).scalars())  # fmt: skip
    wanted_at = case.evidence_wanted_at
    snapshot_id = new_snapshot_id()
    start, end, items = await gather(sources, case.turbine_id, detection_ids, snapshot_id, now)
    async with engine.begin() as connection:
        await in_tenant(connection, tenant)
        current = (await connection.execute(
            select(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
        )).one()  # fmt: skip
        version = current.evidence_version + 1
        lines = [(i.source_service, i.record_type, i.record_id, i.version, i.content_hash)
                 for i in items]  # fmt: skip
        await connection.execute(EVIDENCE_SNAPSHOTS.insert().values(
            tenant_id=tenant, snapshot_id=snapshot_id, case_id=case_id, version=version,
            turbine_id=case.turbine_id, hash_format=HASH_FORMAT, created_at=now, created_by=by,
            content_hash=snapshot_hash(tenant, case_id, version, start, end, lines),
            window_start=start, window_end=end, item_count=len(items),
        ))  # fmt: skip
        await connection.execute(EVIDENCE_ITEMS.insert(), [
            {"tenant_id": tenant, "snapshot_id": snapshot_id, "position": position,
             "source_service": i.source_service, "record_type": i.record_type,
             "record_id": i.record_id, "version": i.version, "window_start": i.window_start,
             "window_end": i.window_end, "retrieved_at": now,
             "data_quality": canonical(i.data_quality or {}), "content": i.content_json,
             "content_hash": i.content_hash}
            for position, i in enumerate(items)
        ])  # fmt: skip
        # A detection that joined while this was frozen still wants a newer one.
        still_wanted = (
            current.evidence_wanted_at is not None and wanted_at is not None
            and _utc(current.evidence_wanted_at) > _utc(wanted_at)
        )  # fmt: skip
        await connection.execute(
            update(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id).values(
                evidence_snapshot_id=snapshot_id, evidence_version=version,
                evidence_wanted_at=current.evidence_wanted_at if still_wanted else None,
                version=current.version + 1, updated_at=now,
            )
        )  # fmt: skip
        changed = (await connection.execute(
            select(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
        )).one()  # fmt: skip
        await record(connection, changed, action="evidence_frozen", at=now,
                     actor_type=actor_type, actor_id=by, note=snapshot_id)  # fmt: skip
    return snapshot_id


async def load(connection: AsyncConnection, tenant: str, snapshot_id: str) -> Any | None:
    s = EVIDENCE_SNAPSHOTS.c
    return (await connection.execute(
        select(EVIDENCE_SNAPSHOTS).where(s.tenant_id == tenant, s.snapshot_id == snapshot_id)
    )).one_or_none()  # fmt: skip


async def items_of(connection: AsyncConnection, tenant: str, snapshot_id: str) -> list[Any]:
    return list((await connection.execute(
        select(EVIDENCE_ITEMS)
        .where(EVIDENCE_ITEMS.c.tenant_id == tenant, EVIDENCE_ITEMS.c.snapshot_id == snapshot_id)
        .order_by(EVIDENCE_ITEMS.c.position)
    )).all())  # fmt: skip


def verified(snapshot: Any, items: Sequence[Any]) -> bool:
    """Every item's hash matches its content, and the snapshot's matches them all."""
    if any(hashlib.sha256(i.content.encode()).hexdigest() != i.content_hash for i in items):
        return False
    lines = [(i.source_service, i.record_type, i.record_id, i.version, i.content_hash)
             for i in items]  # fmt: skip
    expected = snapshot_hash(
        snapshot.tenant_id, snapshot.case_id, snapshot.version, _utc(snapshot.window_start),
        _utc(snapshot.window_end), lines,
    )  # fmt: skip
    return bool(snapshot.content_hash == expected)


class Builder:
    """Freezes evidence for each active case that wants it, as the Investigation Service."""

    def __init__(self, engine: AsyncEngine, sources: Sources, secret: str) -> None:
        self.engine, self.sources, self.secret = engine, sources, secret

    async def run_once(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        async with self.engine.connect() as connection:
            await across_tenants(connection)
            wanted = (await connection.execute(
                select(CASES.c.tenant_id, CASES.c.case_id)
                .where(CASES.c.evidence_wanted_at.is_not(None), CASES.c.status.in_(ACTIVE))
                .order_by(CASES.c.evidence_wanted_at)
            )).all()  # fmt: skip
        frozen = 0
        for tenant, case_id in wanted:
            try:
                with acting_as_service(SERVICE_NAME, tenant, self.secret):
                    await freeze(self.engine, self.sources, tenant, case_id, BUILDER, "service",
                                 now)  # fmt: skip
                frozen += 1
            except DomainError as error:
                # A source cannot be reached: the evidence stays pending, and is tried again.
                logger.warning(
                    "Could not freeze a case's evidence",
                    extra={"case_id": case_id, "error_type": type(error).__name__},
                )
        return frozen


async def keep_building(builder: Builder, stop: asyncio.Event, every_seconds: float = 15.0) -> None:
    while not stop.is_set():
        try:
            done = await builder.run_once()
            if done:
                logger.info("Froze evidence", extra={"cases": done})
        except Exception as error:  # Tried again shortly. The cases are safe meanwhile.
            logger.warning("Could not run the evidence builder",
                           extra={"error_type": type(error).__name__})  # fmt: skip
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)
