"""Reading telemetry and snapshots (FR-TEL-006, SRS 7.4 and 8.4), through a real gRPC server and SQL
database, with stand-ins for the Authorization and Asset services."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.telemetry.v1 import telemetry_pb2 as pb
from windops.telemetry.v1 import telemetry_pb2_grpc
from windops_grpc import (
    NotFoundError,
    ServiceRuntime,
    Settings,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_outbox import OUTBOX_TABLE, create_outbox_table
from windops_telemetry import snapshots
from windops_telemetry.service import TelemetryService
from windops_telemetry.store import (
    METADATA,
    OPERATIONAL_SAMPLES,
    RETENTION_HOLDS,
    SERVICE_NAME,
)

SECRET = "x" * 32
TENANT = "north-sea"
SETTINGS = Settings(
    environment="test", version="test", identity_secret=SECRET, grpc_port=0, http_port=0,
    shutdown_grace_seconds=1,
)  # fmt: skip
FARMS = {"dogger-east-t01": "dogger-east"}


@dataclass
class FakeAuthorizer:
    """Grants (user, permission, wind farm). An empty wind farm means the whole tenant."""

    grants: set[tuple[str, str, str]] = field(default_factory=set)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        user = current_identity().user_id or ""
        return (user, permission, "") in self.grants or (
            bool(wind_farm_id) and (user, permission, wind_farm_id) in self.grants
        )


class FakeTurbines:
    async def wind_farm_of(self, turbine_id: str) -> str:
        if turbine_id not in FARMS:
            raise NotFoundError("No such turbine")
        return FARMS[turbine_id]


def as_user(user: str) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=TENANT, user_id=user)
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


TURBINE = "dogger-east-t01"
# A window of 10 samples, 1 Hz, an hour ago.
START = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
NOW = START + timedelta(hours=1)


@dataclass
class World:
    stub: Any
    engine: AsyncEngine


def stamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value)
    return result


def stored(sequence: int, label: str = "ok", *, ingested: datetime | None = None, index: int = 0,
           tenant: str = TENANT, **readings: float | None) -> dict[str, Any]:  # fmt: skip
    values = dict.fromkeys(snapshots.READING_ORDER, 1.0) | {"gearbox_oil_temp_c": 60.0 + sequence}
    return {
        "tenant_id": tenant, "turbine_id": TURBINE, "sequence": sequence,
        "event_time": START + timedelta(seconds=sequence), "device_id": "dev_1",
        "ingested_at": ingested or START + timedelta(seconds=sequence + 2), "arrival_index": index,
        "source": "windops-simulator", "schema_version": "2", "model_version": "sim-1.4",
        "synthetic": True, "operating_state": "producing", "available": True,
        "quality_status": label, **values, **readings,
    }  # fmt: skip


def with_service(scenario: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
            rows = [stored(n) for n in range(10)]
            rows[3] = stored(3, "delayed")
            rows[4] = stored(4, gearbox_oil_temp_c=None)  # the sensor sent nothing
            # A second copy of 5, arriving later, which M4.6 labels a duplicate.
            rows.append(stored(5, "duplicate", ingested=START + timedelta(seconds=30)))
            # Another tenant's sample in the same window, which row-level security hides.
            rows.append(stored(6, tenant="atlas-wind"))
            await connection.execute(OPERATIONAL_SAMPLES.insert(), rows)
        authorizer = FakeAuthorizer(
            {("u-reader", "telemetry.read", "dogger-east"), ("u-other", "asset.manage", "")}
        )

        def register(server: grpc.aio.Server) -> None:
            telemetry_pb2_grpc.add_TelemetryServiceServicer_to_server(
                TelemetryService(engine, authorizer, FakeTurbines(), version="1.2.3"), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                await scenario(World(telemetry_pb2_grpc.TelemetryServiceStub(channel), engine))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def stream_request(
    *metrics: pb.Metric.ValueType, hours: float = 1
) -> pb.StreamTurbineTelemetryRequest:
    return pb.StreamTurbineTelemetryRequest(
        turbine_id=TURBINE, from_time=stamp(START), to_time=stamp(START + timedelta(hours=hours)),
        metrics=list(metrics),
    )  # fmt: skip


def create_request(key: str = "k1") -> pb.CreateTelemetrySnapshotRequest:
    return pb.CreateTelemetrySnapshotRequest(
        turbine_id=TURBINE, from_time=stamp(START), to_time=stamp(START + timedelta(minutes=1)),
        idempotency_key=key,
    )  # fmt: skip


async def fetch(world: World, snapshot_id: str) -> Any:
    request = pb.GetTelemetrySnapshotRequest(snapshot_id=snapshot_id)
    answer = await world.stub.GetTelemetrySnapshot(
        request, metadata=as_user("u-reader"), timeout=10
    )
    return answer.snapshot


async def gather(call: Any) -> list[Any]:
    return [item async for item in call]


def test_a_turbines_telemetry_streams_in_order_with_units_and_quality() -> None:
    async def scenario(world: World) -> None:
        call = world.stub.StreamTurbineTelemetry(
            stream_request(pb.METRIC_GEARBOX_TEMPERATURE), metadata=as_user("u-reader"), timeout=5
        )
        points = await gather(call)
        # Ten samples, each once: the duplicate copy of 5 is left out, and atlas-wind's is hidden.
        assert [p.sequence for p in points] == list(range(10))
        assert {p.unit for p in points} == {"degC"}
        assert points[0].value == 60.0 and points[0].quality == pb.QUALITY_GOOD
        assert (points[3].quality, points[3].quality_label) == (pb.QUALITY_SUSPECT, "delayed")
        assert (points[4].quality, points[4].value) == (pb.QUALITY_MISSING, 0.0)
        every = await gather(
            world.stub.StreamTurbineTelemetry(
                stream_request(), metadata=as_user("u-reader"), timeout=10
            )
        )
        assert len(every) == 10 * len(snapshots.METRICS)

    with_service(scenario)


def test_reading_needs_telemetry_read_for_the_turbine_and_a_bounded_window() -> None:
    async def scenario(world: World) -> None:
        async def code(request: Any, user: str = "u-reader") -> grpc.StatusCode:
            call = world.stub.StreamTurbineTelemetry(request, metadata=as_user(user), timeout=5)
            return await code_of(gather(call))

        assert await code(stream_request(), "u-other") == grpc.StatusCode.PERMISSION_DENIED
        assert await code(stream_request(hours=25)) == grpc.StatusCode.INVALID_ARGUMENT
        unknown = stream_request()
        unknown.turbine_id = "nowhere-t01"
        assert await code(unknown) == grpc.StatusCode.NOT_FOUND
        bad_metric = stream_request()
        bad_metric.metrics.append(99)  # type: ignore[arg-type]
        assert await code(bad_metric) == grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_a_snapshot_freezes_its_window_with_a_hash_a_manifest_and_a_hold() -> None:
    async def scenario(world: World) -> None:
        reader = as_user("u-reader")
        made = await world.stub.CreateTelemetrySnapshot(
            create_request(), metadata=reader, timeout=10
        )
        assert made.snapshot_id.startswith("tsnap_") and len(made.content_hash) == 64
        assert made.sample_count == 10 and made.created_by == "u-reader"
        manifest = made.manifest
        assert manifest.hash_format == snapshots.HASH_FORMAT and manifest.service_version == "1.2.3"
        assert [(s.source, s.model_version, s.samples) for s in manifest.sources] == [
            ("windops-simulator", "sim-1.4", 10)
        ]
        assert {q.label: q.samples for q in manifest.quality} == {"ok": 9, "delayed": 1}
        assert (manifest.first_sequence, manifest.last_sequence) == (0, 9)
        # Retrying with the same key gives the same snapshot, not a second one.
        again = await world.stub.CreateTelemetrySnapshot(
            create_request(), metadata=reader, timeout=10
        )
        assert again.snapshot_id == made.snapshot_id
        # Another key freezes the same data, so the hash is the same: it depends on the data only.
        other = await world.stub.CreateTelemetrySnapshot(
            create_request("k2"), metadata=reader, timeout=10
        )
        assert other.snapshot_id != made.snapshot_id and other.content_hash == made.content_hash
        async with world.engine.connect() as connection:
            holds = (await connection.execute(select(RETENTION_HOLDS))).all()
            events = (await connection.execute(select(OUTBOX_TABLE.c.subject))).scalars().all()
        assert len(holds) == 2 and all(h.released_at is None for h in holds)
        assert {(h.table_name, h.starts_at.replace(tzinfo=UTC)) for h in holds} == {
            ("operational_samples", START)
        }
        assert len(events) == 2  # each snapshot made is audited

    with_service(scenario)


def test_late_data_and_new_labels_never_change_a_snapshot() -> None:
    """EVD-004: it is frozen. A live read sees the change; the snapshot does not."""

    async def scenario(world: World) -> None:
        reader = as_user("u-reader")
        made = await world.stub.CreateTelemetrySnapshot(
            create_request(), metadata=reader, timeout=10
        )
        async with world.engine.begin() as connection:
            # A sample for the window arrives late, and a label changes on a later look.
            late = stored(20, ingested=datetime.now(UTC) + timedelta(seconds=5))
            late["event_time"] = START + timedelta(seconds=11)
            await connection.execute(OPERATIONAL_SAMPLES.insert(), [late])
            await connection.execute(
                update(OPERATIONAL_SAMPLES)
                .where(OPERATIONAL_SAMPLES.c.sequence == 0)
                .values(quality_status="out_of_order")
            )
        got = await fetch(world, made.snapshot_id)
        assert got.verified and got.content_hash == made.content_hash and got.sample_count == 10
        frozen = await gather(world.stub.StreamTelemetrySnapshot(
            pb.StreamTelemetrySnapshotRequest(
                snapshot_id=made.snapshot_id, metrics=[pb.METRIC_GEARBOX_TEMPERATURE]
            ), metadata=reader, timeout=10,
        ))  # fmt: skip
        assert [r.point.sequence for r in frozen] == list(range(10))
        assert frozen[0].point.quality_label == "ok"  # as it was when frozen
        live = await gather(world.stub.StreamTurbineTelemetry(
            stream_request(pb.METRIC_GEARBOX_TEMPERATURE), metadata=reader, timeout=10
        ))  # fmt: skip
        assert len(live) == 11 and live[0].quality_label == "out_of_order"

    with_service(scenario)


def test_changed_data_fails_verification() -> None:
    async def scenario(world: World) -> None:
        reader = as_user("u-reader")
        made = await world.stub.CreateTelemetrySnapshot(
            create_request(), metadata=reader, timeout=10
        )
        async with world.engine.begin() as connection:
            await connection.execute(
                update(OPERATIONAL_SAMPLES)
                .where(OPERATIONAL_SAMPLES.c.sequence == 2)
                .values(gearbox_oil_temp_c=99.0)
            )
        got = await fetch(world, made.snapshot_id)
        assert not got.verified

    with_service(scenario)


def test_a_snapshot_needs_a_key_a_finished_window_and_the_permission() -> None:
    async def scenario(world: World) -> None:
        async def code(request: Any, user: str = "u-reader") -> grpc.StatusCode:
            call = world.stub.CreateTelemetrySnapshot(request, metadata=as_user(user), timeout=5)
            return await code_of(call)

        assert await code(create_request(key="")) == grpc.StatusCode.INVALID_ARGUMENT
        future = create_request()
        future.to_time.FromDatetime(datetime.now(UTC) + timedelta(hours=1))
        future.from_time.FromDatetime(datetime.now(UTC))
        assert await code(future) == grpc.StatusCode.INVALID_ARGUMENT
        assert await code(create_request(), "u-other") == grpc.StatusCode.PERMISSION_DENIED
        await world.stub.CreateTelemetrySnapshot(
            create_request(), metadata=as_user("u-reader"), timeout=10
        )
        reused = create_request()
        reused.to_time.FromDatetime(START + timedelta(minutes=2))
        assert await code(reused) == grpc.StatusCode.FAILED_PRECONDITION  # same key, new request

    with_service(scenario)


def test_releasing_a_snapshot_lets_its_hold_go_once() -> None:
    async def scenario(world: World) -> None:
        reader = as_user("u-reader")
        made = await world.stub.CreateTelemetrySnapshot(
            create_request(), metadata=reader, timeout=10
        )
        release = pb.ReleaseTelemetrySnapshotRequest(
            snapshot_id=made.snapshot_id, idempotency_key="r1"
        )
        released = (
            await world.stub.ReleaseTelemetrySnapshot(release, metadata=reader, timeout=10)
        ).snapshot
        assert released.HasField("released_at")
        again = (
            await world.stub.ReleaseTelemetrySnapshot(release, metadata=reader, timeout=10)
        ).snapshot
        assert again.released_at == released.released_at  # a retry, answered the same
        twice = pb.ReleaseTelemetrySnapshotRequest(
            snapshot_id=made.snapshot_id, idempotency_key="r2"
        )
        assert await code_of(
            world.stub.ReleaseTelemetrySnapshot(twice, metadata=reader, timeout=10)
        ) == grpc.StatusCode.FAILED_PRECONDITION  # fmt: skip
        async with world.engine.connect() as connection:
            hold = (await connection.execute(select(RETENTION_HOLDS))).one()
        assert hold.released_at is not None
        # Still readable, and still verified, until retention removes its data.
        got = await fetch(world, made.snapshot_id)
        assert got.verified and got.HasField("released_at")

    with_service(scenario)


def test_an_unknown_snapshot_is_not_found() -> None:
    async def scenario(world: World) -> None:
        call = world.stub.GetTelemetrySnapshot(
            pb.GetTelemetrySnapshotRequest(snapshot_id="tsnap_nope"),
            metadata=as_user("u-reader"),
            timeout=10,
        )
        assert await code_of(call) == grpc.StatusCode.NOT_FOUND

    with_service(scenario)


def test_the_content_hash_can_be_written_again_from_its_documented_format() -> None:
    """Anyone with the samples can check a snapshot's hash, without this service."""
    import hashlib

    sample = snapshots.Sample(
        sequence=7, event_time=START, ingested_at=START, arrival_index=0, label="ok",
        source="s", schema_version="2", model_version="m", synthetic=True,
        readings={r: (None if r == "power_kw" else 1.5) for r in snapshots.READING_ORDER},
    )  # fmt: skip
    end = START + timedelta(minutes=1)
    by_hand = hashlib.sha256()
    by_hand.update(
        f"{snapshots.HASH_FORMAT}|t|turbine|{START.isoformat()}|{end.isoformat()}\n".encode()
    )
    numbers = ["null" if reading == "power_kw" else "1.5" for reading in snapshots.READING_ORDER]
    by_hand.update(("|".join([START.isoformat(), "7", "ok", *numbers]) + "\n").encode())
    assert snapshots.content_hash("t", "turbine", START, end, [sample]) == by_hand.hexdigest()
    assert list(snapshots.first_copies([sample, sample])) == [sample]
