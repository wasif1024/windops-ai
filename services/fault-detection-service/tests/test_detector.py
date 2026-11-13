"""The detector, against SQLite, with stand-ins for the services it reads as itself (FR-FLT-003)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, parse_event
from windops_fault_detection import engine
from windops_fault_detection.detector import SERVICE_SUBJECT, Detector, watch
from windops_fault_detection.store import DETECTIONS, METADATA
from windops_grpc import UnavailableError, current_identity
from windops_outbox import OUTBOX_TABLE, create_outbox_table

TENANT, TURBINE = "north-sea", "dogger-east-t01"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
INTERVAL = timedelta(minutes=5)


@dataclass
class Turbine:
    @dataclass
    class Model:
        rated_power_kw: float = 8000.0
        rotor_diameter_m: float = 167.0

    model: Model = field(default_factory=Model)
    wind_farm_id: str = "dogger-east"


@dataclass
class FakeSources:
    """A turbine at full load in 10 °C air, its gearbox oil `heat` °C above what that explains."""

    heat: float = 0.0
    snapshots_fail: bool = False
    callers: list[str] = field(default_factory=list)
    snapshots: list[tuple[str, str]] = field(default_factory=list)
    fetched: list[timedelta] = field(default_factory=list)

    def _who(self) -> None:
        self.callers.append(current_identity().subject)

    async def turbine(self, turbine_id: str) -> Any:
        self._who()
        return Turbine()

    async def samples(self, turbine_id: str, start: datetime, end: datetime,
                      readings: list[str]) -> list[Any]:  # fmt: skip
        self._who()
        self.fetched.append(end - start)
        reading = {"power_kw": 8000.0, "ambient_temp_c": 10.0,
                   "gearbox_oil_temp_c": 63.0 + self.heat, "gearbox_bearing_temp_c": 73.0,
                   "main_bearing_temp_c": 32.0, "generator_bearing_temp_c": 63.0,
                   "nacelle_vibration_mm_s": 1.0, "rotor_speed_rpm": 6.0}  # fmt: skip
        seconds = int((end - start).total_seconds())
        return [engine.Sample(start + timedelta(seconds=i + 1), reading) for i in range(seconds)]

    async def alarms(self, turbine_id: str) -> list[Any]:
        self._who()
        return []

    async def snapshot(self, turbine_id: str, start: datetime, end: datetime, key: str) -> Any:
        self._who()
        if self.snapshots_fail:
            raise UnavailableError("Telemetry cannot be reached right now")
        self.snapshots.append((turbine_id, key))

        @dataclass
        class Snapshot:
            snapshot_id: str = f"tsnap_{key[-20:]}"
            content_hash: str = "a" * 64

        return Snapshot()


@dataclass
class World:
    engine: AsyncEngine
    sources: FakeSources
    detector: Detector

    async def detections(self) -> list[Any]:
        async with self.engine.connect() as connection:
            return list((await connection.execute(
                select(DETECTIONS).order_by(DETECTIONS.c.detected_at)
            )).all())  # fmt: skip

    async def events(self) -> list[Any]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        return [parse_event(row.subject, row.envelope) for row in rows]

    async def announce(self, at: datetime, producer: str = "telemetry") -> None:
        payload = events_pb2.TelemetryReceived(turbine_id=TURBINE, batch_id="b", sample_count=5)
        await watch(self.engine, Event("telemetry.received", "e", TENANT, at, producer, payload,
                                       "", ""), at)  # fmt: skip


def with_detector(scenario: Callable[[World], Awaitable[None]]) -> None:
    async def run() -> None:
        engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine_.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        sources = FakeSources()
        detector = Detector(engine_, sources, "x" * 32, INTERVAL)
        try:
            await scenario(World(engine_, sources, detector))
        finally:
            await engine_.dispose()

    asyncio.run(run())


def test_a_finding_becomes_a_detection_with_its_rule_version_and_frozen_telemetry() -> None:
    async def scenario(world: World) -> None:
        await world.announce(NOW)
        world.sources.heat = 9.0
        assert await world.detector.run_once(NOW) == 1
        (detection,) = await world.detections()
        assert (detection.rule_id, detection.rule_version, detection.severity, detection.state) == (
            "gearbox-oil-residual", 1, "high", "open",
        )  # fmt: skip
        assert len(detection.rule_content_hash) == 64
        assert detection.telemetry_snapshot_id.startswith("tsnap_")
        assert json.loads(detection.observed)["least_residual"] == pytest.approx(9.0)
        assert world.sources.snapshots == [(TURBINE, detection.detection_id)]
        # Every call went out as the service itself, never as a person.
        assert set(world.sources.callers) == {SERVICE_SUBJECT}
        events = await world.events()
        (detected,) = [e for e in events if e.subject == "fault.detected"]
        payload: Any = detected.payload
        assert (payload.detection_id, payload.rule_id, payload.severity, payload.rule_version) == (
            detection.detection_id, "gearbox-oil-residual", "high", "1",
        )  # fmt: skip
        assert payload.telemetry_snapshot_id == detection.telemetry_snapshot_id
        audited: list[Any] = [
            e.payload for e in events if e.subject == "audit.requested.fault-detection"
        ]
        assert ("fault.detected", SERVICE_SUBJECT) in {(a.action, a.actor_id) for a in audited}

    with_detector(scenario)


def test_a_detection_stays_open_while_found_is_replaced_on_change_and_closes_when_gone() -> None:
    async def scenario(world: World) -> None:
        world.sources.heat = 9.0
        for minutes in (0, 5):
            await world.announce(NOW + timedelta(minutes=minutes))
            await world.detector.run_once(NOW + timedelta(minutes=minutes))
        (first,) = await world.detections()
        assert first.last_seen_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=5)
        # Hotter: once that holds for the rule's whole period, a critical detection of its own.
        world.sources.heat = 16.0
        await world.announce(NOW + timedelta(minutes=20))
        await world.detector.run_once(NOW + timedelta(minutes=20))
        old, new = [d for d in await world.detections() if d.rule_id == "gearbox-oil-residual"]
        assert (old.state, old.closed_reason) == ("closed", "replaced")
        assert (new.severity, new.state) == ("critical", "open")
        assert new.telemetry_snapshot_id != old.telemetry_snapshot_id
        # Healthy again: it closes, and nothing is detected.
        world.sources.heat = 0.0
        await world.announce(NOW + timedelta(minutes=35))
        await world.detector.run_once(NOW + timedelta(minutes=35))
        residual = [d for d in await world.detections() if d.rule_id == "gearbox-oil-residual"]
        assert [(d.state, d.closed_reason) for d in residual] == [
            ("closed", "replaced"), ("closed", "cleared"),
        ]  # fmt: skip
        # The safety limit it also crossed at 79 °C closes too, and nothing is left open.
        assert {d.state for d in await world.detections()} == {"closed"}

    with_detector(scenario)


def test_without_a_frozen_snapshot_there_is_no_detection_until_there_is() -> None:
    """SRS 7.7: when Telemetry is down, refuse a new snapshot, and so a new detection."""

    async def scenario(world: World) -> None:
        world.sources.heat, world.sources.snapshots_fail = 9.0, True
        await world.announce(NOW)
        await world.detector.run_once(NOW)
        assert await world.detections() == []
        assert not [e for e in await world.events() if e.subject == "fault.detected"]
        world.sources.snapshots_fail = False
        await world.announce(NOW + INTERVAL)
        await world.detector.run_once(NOW + INTERVAL)
        assert len(await world.detections()) == 1

    with_detector(scenario)


def test_only_streaming_turbines_are_evaluated_and_each_once_an_interval() -> None:
    async def scenario(world: World) -> None:
        await world.announce(NOW)
        assert await world.detector.run_once(NOW) == 1
        assert await world.detector.run_once(NOW + timedelta(minutes=2)) == 0
        # Its telemetry stopped long ago, so it is not evaluated again.
        assert await world.detector.run_once(NOW + timedelta(hours=1)) == 0
        with pytest.raises(InvalidEventError):
            await world.announce(NOW, producer="simulator")

    with_detector(scenario)


def test_after_its_first_run_a_turbine_fetches_only_what_is_new() -> None:
    """A run reads the whole window once; later runs read the interval and an overlap."""

    async def scenario(world: World) -> None:
        for minutes in (0, 5, 10):
            await world.announce(NOW + timedelta(minutes=minutes))
            await world.detector.run_once(NOW + timedelta(minutes=minutes))
        first, *later = world.sources.fetched
        assert first > timedelta(hours=2)
        assert later == [INTERVAL + timedelta(minutes=2)] * 2
        ((_, buffer),) = world.detector.buffers.items()
        assert len(buffer.samples) == int(first.total_seconds())

    with_detector(scenario)
