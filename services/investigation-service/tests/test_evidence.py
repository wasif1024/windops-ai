"""Evidence snapshots: frozen, hashed and versioned with their case (FR-INV-002, EVD-001 to 004)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from test_windops_investigation import World, as_user, code_of, with_service
from windops.alarm.v1 import alarm_pb2
from windops.asset.v1 import asset_pb2
from windops.events.v1 import events_pb2
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.investigation.v1 import investigation_pb2 as pb
from windops.telemetry.v1 import telemetry_pb2
from windops_events import Event
from windops_grpc import UnavailableError, current_identity
from windops_investigation import evidence
from windops_investigation.consumer import handle
from windops_investigation.store import CASES, EVIDENCE_ITEMS, EVIDENCE_SNAPSHOTS, METADATA
from windops_outbox import create_outbox_table

TENANT, TURBINE = "north-sea", "dogger-east-t01"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def ts(value: datetime) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


@dataclass
class FakeSources:
    down: set[str] = field(default_factory=set)
    callers: list[str] = field(default_factory=list)
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)

    def _ask(self, owner: str) -> None:
        self.callers.append(current_identity().subject)
        if owner in self.down:
            raise UnavailableError(f"{owner} cannot be reached right now")

    async def turbine(self, turbine_id: str) -> Any:
        self._ask("asset")
        return asset_pb2.Turbine(turbine_id=turbine_id, wind_farm_id="dogger-east", version=3)

    async def telemetry_snapshot(self, turbine_id: str, start: datetime, end: datetime,
                                 key: str) -> Any:  # fmt: skip
        self._ask("telemetry")
        self.windows.append((start, end))
        return telemetry_pb2.TelemetrySnapshot(
            snapshot_id=f"tsnap_{key[-12:]}", content_hash="t" * 64, turbine_id=turbine_id,
            sample_count=7200, from_time=ts(start), to_time=ts(end),
            manifest=telemetry_pb2.SnapshotManifest(
                quality=[telemetry_pb2.QualityCount(label="ok", samples=7190)], rejected=1
            ),
        )  # fmt: skip

    async def alarms(self, turbine_id: str) -> list[Any]:
        self._ask("alarm")
        return [
            alarm_pb2.Alarm(alarm_id="alm_in", turbine_id=turbine_id, code="GBX_OIL_TEMP_HIGH",
                            raised_at=ts(NOW - timedelta(hours=1)), version=2),
            # Cleared long before the window: not part of this case's evidence.
            alarm_pb2.Alarm(alarm_id="alm_old", turbine_id=turbine_id, code="X_Y",
                            raised_at=ts(NOW - timedelta(days=3)),
                            cleared_at=ts(NOW - timedelta(days=2)), version=2),
        ]  # fmt: skip

    async def detection(self, detection_id: str) -> Any:
        self._ask("fault-detection")
        return fd.Detection(
            detection_id=detection_id, turbine_id=TURBINE, rule_id="gearbox-oil-residual",
            rule_version=1, rule_content_hash="r" * 64, severity="high",
            window_start=ts(NOW - timedelta(hours=2)), window_end=ts(NOW),
            telemetry_snapshot_id=f"tsnap_{detection_id}",
        )  # fmt: skip

    async def rule_version(self, rule_id: str, version: int) -> Any:
        self._ask("fault-detection")
        return fd.FaultRuleVersion(
            rule_id=rule_id, version=version, kind="residual",
            definition_json='{"kind":"residual"}', content_hash="r" * 64,
        )  # fmt: skip


@dataclass
class Bench:
    engine: AsyncEngine
    sources: FakeSources
    builder: evidence.Builder

    async def detect(self, detection_id: str, severity: str = "high") -> None:
        payload = events_pb2.FaultDetected(
            detection_id=detection_id, turbine_id=TURBINE, rule_version="1", severity=severity,
            telemetry_snapshot_id=f"tsnap_{detection_id}", rule_id="gearbox-oil-residual",
            rule_content_hash="r" * 64,
        )  # fmt: skip
        await handle(self.engine, Event("fault.detected", f"e-{detection_id}", TENANT, NOW,
                                        "fault-detection", payload, "", ""))  # fmt: skip

    async def case(self) -> Any:
        async with self.engine.connect() as connection:
            return (await connection.execute(select(CASES))).one()

    async def snapshots(self) -> list[Any]:
        async with self.engine.connect() as connection:
            return list((await connection.execute(
                select(EVIDENCE_SNAPSHOTS).order_by(EVIDENCE_SNAPSHOTS.c.version)
            )).all())  # fmt: skip

    async def items(self, snapshot_id: str) -> list[Any]:
        async with self.engine.connect() as connection:
            return await evidence.items_of(connection, TENANT, snapshot_id)


def with_bench(scenario: Callable[[Bench], Awaitable[None]]) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        sources = FakeSources()
        try:
            await scenario(Bench(engine, sources, evidence.Builder(engine, sources, "x" * 32)))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_a_new_case_is_bound_to_frozen_evidence_of_everything_it_rests_on() -> None:
    async def scenario(bench: Bench) -> None:
        await bench.detect("d1")
        assert (await bench.case()).evidence_wanted_at is not None, "pending at first"
        assert await bench.builder.run_once(NOW) == 1
        case = await bench.case()
        (snapshot,) = await bench.snapshots()
        assert (case.evidence_snapshot_id, case.evidence_version, case.evidence_wanted_at) == (
            snapshot.snapshot_id, 1, None,
        )  # fmt: skip
        items = await bench.items(snapshot.snapshot_id)
        assert [(i.source_service, i.record_type, i.record_id) for i in items] == [
            ("asset", "turbine", TURBINE),
            ("telemetry", "telemetry_snapshot", f"tsnap_{snapshot.snapshot_id[-12:]}"),
            ("alarm", "alarm", "alm_in"),
            ("fault-detection", "detection", "d1"),
            ("fault-detection", "fault_rule_version", "gearbox-oil-residual@1"),
        ]
        telemetry = items[1]
        assert json.loads(telemetry.data_quality) == {
            "quality": {"ok": 7190}, "gaps": 0, "rejected": 1, "samples": 7200,
        }  # fmt: skip
        # The window starts with the earliest detection's, so it covers what was found.
        ((start, _),) = bench.sources.windows
        assert start == NOW - timedelta(hours=2)
        assert evidence.verified(snapshot, items)
        assert set(bench.sources.callers) == {"service:investigation"}

    with_bench(scenario)


def test_a_joining_detection_brings_a_new_version_and_the_old_one_stays() -> None:
    async def scenario(bench: Bench) -> None:
        await bench.detect("d1")
        await bench.builder.run_once(NOW)
        await bench.detect("d2")
        await bench.builder.run_once(NOW + timedelta(minutes=1))
        first, second = await bench.snapshots()
        assert (first.version, second.version) == (1, 2)
        assert (await bench.case()).evidence_snapshot_id == second.snapshot_id
        assert [i.record_id for i in await bench.items(second.snapshot_id)
                if i.record_type == "detection"] == ["d1", "d2"]  # fmt: skip
        assert evidence.verified(first, await bench.items(first.snapshot_id))

    with_bench(scenario)


def test_without_every_source_there_is_no_snapshot_and_evidence_stays_pending() -> None:
    """SRS 7.7: Telemetry or Alarm down, refuse a new snapshot. Never a partial one."""

    async def scenario(bench: Bench) -> None:
        await bench.detect("d1")
        bench.sources.down = {"alarm"}
        assert await bench.builder.run_once(NOW) == 0
        assert await bench.snapshots() == []
        assert (await bench.case()).evidence_wanted_at is not None
        bench.sources.down = set()
        assert await bench.builder.run_once(NOW) == 1

    with_bench(scenario)


def test_a_changed_item_no_longer_verifies() -> None:
    """EVD-001: PostgreSQL refuses the change; were it made anyway, the hashes would show it."""

    async def scenario(bench: Bench) -> None:
        await bench.detect("d1")
        await bench.builder.run_once(NOW)
        (snapshot,) = await bench.snapshots()
        async with bench.engine.begin() as connection:
            await connection.execute(
                update(EVIDENCE_ITEMS)
                .where(EVIDENCE_ITEMS.c.record_type == "alarm")
                .values(content='{"alarm_id":"alm_in","code":"SOMETHING_ELSE"}')
            )
        assert not evidence.verified(snapshot, await bench.items(snapshot.snapshot_id))

    with_bench(scenario)


def test_a_person_freezes_evidence_and_reads_it_back_verified() -> None:
    async def scenario(world: World) -> None:
        await world.detect("d1", "gearbox-oil-residual", "high")
        (case,) = await world.active()
        assert (case.evidence_status, case.evidence_snapshot_id) == ("pending", "")
        request = pb.CreateEvidenceSnapshotRequest(case_id=case.case_id, idempotency_key="e1")
        made = (await world.stub.CreateEvidenceSnapshot(request, metadata=as_user(),
                                                        timeout=10)).snapshot  # fmt: skip
        assert made.verified and made.created_by == "u-operator" and made.item_count == 5
        again = (await world.stub.CreateEvidenceSnapshot(request, metadata=as_user(),
                                                         timeout=10)).snapshot  # fmt: skip
        assert again.snapshot_id == made.snapshot_id, "a retry is the same snapshot"
        read = await world.stub.GetEvidenceSnapshot(
            pb.GetEvidenceSnapshotRequest(snapshot_id=made.snapshot_id),
            metadata=as_user("u-auditor"),
            timeout=5,
        )
        assert read.snapshot.verified and len(read.snapshot.items) == 5
        assert await code_of(world.stub.CreateEvidenceSnapshot(
            pb.CreateEvidenceSnapshotRequest(case_id=case.case_id),
            metadata=as_user("u-auditor"), timeout=5,
        )) == grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        listed = await world.stub.ListEvidenceSnapshots(
            pb.ListEvidenceSnapshotsRequest(case_id=case.case_id), metadata=as_user(), timeout=5
        )
        assert [s.version for s in listed.snapshots] == [1]
        (now,) = await world.active()
        assert (now.evidence_status, now.evidence_version) == ("ready", 1)

    with_service(scenario, sources=FakeSources())
