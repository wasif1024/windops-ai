"""Telemetry freshness: each sample's delay as it arrives, and each turbine's age (M4.9)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from test_telemetry_ingest import RECEIVED, TENANT, URL, add_credential, samples
from test_telemetry_ingest import FakeRedis as Redis
from windops_telemetry.freshness import FreshnessWatcher, IngestMetrics, state_of
from windops_telemetry.ingest import create_ingest_router
from windops_telemetry.limits import DeviceLimiter
from windops_telemetry.store import METADATA, OPERATIONAL_SAMPLES

NOW = datetime.fromisoformat("2026-01-15T12:00:00+00:00")


def points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    assert data is not None
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def with_engine(scenario: Callable[[AsyncEngine], Awaitable[None]]) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
        try:
            await scenario(engine)
        finally:
            await engine.dispose()

    asyncio.run(run())


async def store_sample(engine: AsyncEngine, tenant: str, turbine: str, at: datetime) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            OPERATIONAL_SAMPLES.insert().values(
                tenant_id=tenant, turbine_id=turbine, event_time=at, sequence=1,
                device_id="d", ingested_at=at, source="windops-simulator",
                schema_version="1", model_version="1", synthetic=True,
                operating_state="producing", available=True, quality_status="received",
            )
        )  # fmt: skip


def test_a_turbine_is_live_then_delayed_then_stale_and_silent_with_nothing() -> None:
    assert [state_of(age) for age in (0, 60, 61, 300, 301, None)] == [
        "live", "live", "delayed", "delayed", "stale", "silent",
    ]  # fmt: skip


def test_the_device_route_records_each_samples_delay_and_what_it_refused() -> None:
    reader = InMemoryMetricReader()
    measures = IngestMetrics(MeterProvider(metric_readers=[reader]))

    async def scenario(engine: AsyncEngine) -> None:
        token = await add_credential(engine)
        limiter = DeviceLimiter(Redis(), clock=lambda: 1_800_000_030.0)
        app = FastAPI()
        app.include_router(
            create_ingest_router(engine, limiter, clock=lambda: RECEIVED, measures=measures)
        )
        sent = samples(3)
        wrong = {**samples(1)[0], "turbine_id": "hornsea-west-t01"}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://ingest") as client:
            answer = await client.post(
                URL, json={"samples": [*sent, wrong]}, headers={"Authorization": f"Bearer {token}"}
            )
        assert answer.json()["accepted"] == 3

    with_engine(scenario)
    (delay,) = points(reader, "windops.telemetry.ingest.delay")
    # Measured at START + 1, 2 and 3 s, and all stored at START + 5 s.
    assert dict(delay.attributes) == {"tenant_id": TENANT, "kind": "samples"}
    assert (delay.count, delay.sum) == (3, 4.0 + 3.0 + 2.0)
    counted = {
        (p.attributes["outcome"], p.attributes.get("reason")): p.value
        for p in points(reader, "windops.telemetry.ingest.samples")
    }
    assert counted == {("accepted", None): 3, ("rejected", "wrong_turbine"): 1}


def test_each_expected_turbine_reports_how_old_its_newest_sample_is() -> None:
    reader = InMemoryMetricReader()
    clock = [NOW]
    watcher = FreshnessWatcher(
        None,  # type: ignore[arg-type]
        MeterProvider(metric_readers=[reader]),
        clock=lambda: clock[0],
    )

    async def scenario(engine: AsyncEngine) -> None:
        watcher.engine = engine
        for turbine, ago in (("t01", 5), ("t02", 120), ("t03", 900)):
            await add_credential(engine, turbine=turbine)
            await store_sample(engine, TENANT, turbine, NOW - timedelta(seconds=ago))
            # An older sample does not hide the newest.
            await store_sample(engine, TENANT, turbine, NOW - timedelta(seconds=ago + 30))
        await add_credential(engine, turbine="t04")  # expected, and sent nothing
        await store_sample(engine, TENANT, "t04", NOW - timedelta(days=2))  # long ago
        await add_credential(engine, turbine="t05", revoked=True)  # not expected any more
        await store_sample(engine, TENANT, "t05", NOW - timedelta(seconds=5))
        await add_credential(engine, tenant="atlas-wind", turbine="t01")
        await store_sample(engine, "atlas-wind", "t01", NOW - timedelta(seconds=10))
        # Rotated: two active credentials for one turbine count it once.
        await add_credential(engine, turbine="t01")
        await watcher.run_once()

    with_engine(scenario)
    assert set(watcher.newest) == {
        (TENANT, "t01"), (TENANT, "t02"), (TENANT, "t03"), (TENANT, "t04"), ("atlas-wind", "t01"),
    }  # fmt: skip
    ages = {
        (p.attributes["tenant_id"], p.attributes["turbine_id"]): p.value
        for p in points(reader, "windops.telemetry.sample_age_seconds")
    }
    # A silent turbine has no age, and is counted as silent instead.
    assert ages == {
        (TENANT, "t01"): 5, (TENANT, "t02"): 120, (TENANT, "t03"): 900, ("atlas-wind", "t01"): 10,
    }  # fmt: skip
    states = {
        (p.attributes["tenant_id"], p.attributes["state"]): p.value
        for p in points(reader, "windops.telemetry.turbines")
    }
    assert {key: value for key, value in states.items() if key[0] == TENANT} == {
        (TENANT, "live"): 1, (TENANT, "delayed"): 1, (TENANT, "stale"): 1, (TENANT, "silent"): 1,
    }  # fmt: skip
    # Nothing new arrives, so the age keeps growing between reads of the database.
    clock[0] = NOW + timedelta(minutes=5)
    later = {
        p.attributes["turbine_id"]: p.value
        for p in points(reader, "windops.telemetry.sample_age_seconds")
        if p.attributes["tenant_id"] == TENANT
    }
    assert later["t01"] == 305


def test_the_watcher_keeps_going_when_the_database_cannot_be_read() -> None:
    class Broken:
        def begin(self) -> Any:
            raise ConnectionError("down")

    async def scenario() -> None:
        stop = asyncio.Event()
        watcher = FreshnessWatcher(Broken(), MeterProvider())  # type: ignore[arg-type]
        task = asyncio.create_task(watcher.keep_watching(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert watcher.newest == {}

    asyncio.run(scenario())
