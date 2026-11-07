"""SCADA summaries, late data and retention, with samples from the simulator (FR-TEL-004, SRS 8.3).

SQLite has no partitions, so `make telemetry-storage-check` proves making and dropping them, and
holds, against PostgreSQL.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.simulator import Simulator
from windops_telemetry import partitions
from windops_telemetry.schema import READINGS, parse
from windops_telemetry.store import (
    INGEST_REJECTIONS,
    METADATA,
    OPERATIONAL_SAMPLES,
    SCADA_SUMMARIES,
)
from windops_telemetry.summaries import (
    EXPECTED_SAMPLES,
    GRACE,
    PERIOD,
    Summarizer,
    period_start,
    summarize,
)

START = datetime.fromisoformat("2026-01-15T06:00:00+00:00")
TURBINE = "dogger-east-t01"


def simulated(count: int, turbine: str = TURBINE, start: datetime = START) -> list[dict[str, Any]]:
    """Samples a 1 Hz device sends from just after `start`, the first timed start + 1 s."""
    spec = next(spec for spec in DEMO_TURBINES if spec.turbine_id == turbine)
    simulator = Simulator(7, (spec,), start - timedelta(seconds=1))
    return [dict(simulator.step(1.0)[0].as_dict()) for _ in range(count)]


def row(sample: dict[str, Any], ingested_at: datetime, device: str = "dev_1") -> dict[str, Any]:
    parsed = parse(sample)
    return {
        "tenant_id": parsed.tenant_id,
        "turbine_id": parsed.turbine_id,
        "event_time": parsed.event_time,
        "sequence": parsed.sequence,
        "device_id": device,
        "ingested_at": ingested_at,
        "source": parsed.source,
        "schema_version": parsed.schema_version,
        "model_version": parsed.model_version,
        "synthetic": parsed.synthetic,
        "operating_state": parsed.operating_state,
        "available": parsed.available,
        "quality_status": "received",
        **parsed.readings,
    }


def with_database(scenario: Callable[[AsyncEngine], Awaitable[None]]) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
        try:
            await scenario(engine)
        finally:
            await engine.dispose()

    asyncio.run(run())


async def store(engine: AsyncEngine, rows: list[dict[str, Any]]) -> None:
    async with engine.begin() as connection:
        await connection.execute(OPERATIONAL_SAMPLES.insert(), rows)


async def summaries(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as connection:
        query = select(SCADA_SUMMARIES).order_by(
            SCADA_SUMMARIES.c.tenant_id,
            SCADA_SUMMARIES.c.turbine_id,
            SCADA_SUMMARIES.c.period_start,
        )
        return list((await connection.execute(query)).all())


# --- the summary itself


def namespace(sample: dict[str, Any], ingested_at: datetime = START) -> Any:
    return SimpleNamespace(**row(sample, ingested_at))


def test_a_full_period_summarizes_every_reading_with_four_statistics() -> None:
    samples = [namespace(s) for s in simulated(EXPECTED_SAMPLES)]
    values = summarize(samples, START)
    assert (values["sample_count"], values["coverage"], values["quality_status"]) == (
        600, 1.0, "complete"
    )  # fmt: skip
    powers = [s.power_kw for s in samples]
    assert values["power_kw_mean"] == pytest.approx(sum(powers) / 600)
    assert (values["power_kw_min"], values["power_kw_max"]) == (min(powers), max(powers))
    assert values["energy_kwh"] == pytest.approx(values["power_kw_mean"] / 6)
    assert values["synthetic"] is True
    for reading in READINGS:
        assert values[f"{reading.name}_mean"] is not None, reading.name


def test_duplicates_count_once_and_nulls_are_skipped_but_counted() -> None:
    samples = [namespace(s) for s in simulated(300)]
    samples += samples[:10]  # sent twice
    samples[0].main_bearing_temp_c = None
    values = summarize(samples, START)
    assert (values["sample_count"], values["duplicate_samples"]) == (300, 10)
    assert values["coverage"] == 0.5
    assert values["quality_status"] == "partial"
    assert values["null_readings"] == 1
    present = [s.main_bearing_temp_c for s in samples[:300] if s.main_bearing_temp_c is not None]
    assert values["main_bearing_temp_c_mean"] == pytest.approx(sum(present) / len(present))


def test_wind_direction_averages_round_the_circle() -> None:
    """350 and 10 degrees average to north, not to 180."""
    samples = [namespace(s) for s in simulated(2)]
    samples[0].wind_direction_deg, samples[1].wind_direction_deg = 350.0, 10.0
    values = summarize(samples, START)
    assert min(values["wind_direction_deg_mean"], 360 - values["wind_direction_deg_mean"]) < 0.01
    assert values["wind_direction_deg_std"] == pytest.approx(10.0, abs=0.2)
    assert summarize(samples[:1], START)["quality_status"] == "sparse"


def test_periods_start_on_the_ten_minutes() -> None:
    assert period_start(START + timedelta(minutes=9, seconds=59)) == START
    assert period_start(START + PERIOD) == START + PERIOD


# --- the summarizer


def test_a_closed_period_is_summarized_once_and_again_when_late_data_arrives() -> None:
    """SRS 8.3: a summary is recalculated when late data arrives for its period."""

    async def scenario(engine: AsyncEngine) -> None:
        sent = simulated(EXPECTED_SAMPLES)
        arrived = START + PERIOD + timedelta(seconds=5)
        await store(engine, [row(s, arrived) for s in sent[:-1]])
        summarizer = Summarizer(engine)
        first_run = START + PERIOD + GRACE + timedelta(seconds=5)
        assert await summarizer.run_once(first_run) == [("north-sea", TURBINE, START)]
        (first,) = await summaries(engine)
        assert (first.revision, first.sample_count, first.late_samples) == (1, 599, 0)
        assert await summarizer.run_once(first_run + timedelta(minutes=1)) == []

        late = first_run + timedelta(minutes=3)
        await store(engine, [row(sent[-1], late)])
        assert await summarizer.run_once(late + timedelta(seconds=30)) == [
            ("north-sea", TURBINE, START)
        ]
        (again,) = await summaries(engine)
        assert (again.revision, again.sample_count, again.late_samples) == (2, 600, 1)
        assert again.quality_status == "complete"

    with_database(scenario)


def test_an_open_period_waits_until_it_closes_even_with_no_new_samples() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        sent = simulated(120)
        now = START + timedelta(minutes=2, seconds=5)
        await store(engine, [row(s, now) for s in sent])
        summarizer = Summarizer(engine)
        assert await summarizer.run_once(now) == []
        assert await summarizer.run_once(START + PERIOD + timedelta(seconds=30)) == []  # grace
        done = await summarizer.run_once(START + PERIOD + GRACE + timedelta(seconds=1))
        assert done == [("north-sea", TURBINE, START)]
        (summary,) = await summaries(engine)
        assert summary.quality_status == "sparse"

    with_database(scenario)


def test_each_tenant_and_turbine_gets_its_own_summary() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        arrived = START + PERIOD
        rows = [row(s, arrived) for s in simulated(60)]
        rows += [row(s, arrived) for s in simulated(60, "atlas-ridge-t01")]
        rows += [
            row(s, arrived) for s in simulated(60, start=START + PERIOD - timedelta(seconds=30))
        ]
        await store(engine, rows)
        await Summarizer(engine).run_once(START + 2 * PERIOD + GRACE)
        found = [(s.tenant_id, s.turbine_id, s.period_start.replace(tzinfo=UTC)) for s in
                 await summaries(engine)]  # fmt: skip
        assert found == [
            ("atlas-wind", "atlas-ridge-t01", START),
            ("north-sea", TURBINE, START),
            ("north-sea", TURBINE, START + PERIOD),
        ]

    with_database(scenario)


# --- partitions and retention


def test_partitions_are_named_and_planned_by_day_and_by_month() -> None:
    samples, months = partitions.tables()
    assert samples.name_for(date(2026, 1, 5)) == "operational_samples_20260105"
    assert months.name_for(date(2026, 1, 1)) == "scada_summaries_202601"
    assert samples.wanted(date(2026, 1, 1)) == [
        date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)
    ]  # fmt: skip
    assert months.wanted(date(2026, 12, 15)) == [
        date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1), date(2027, 2, 1)
    ]  # fmt: skip
    assert months.start_from_name("scada_summaries_202602") == date(2026, 2, 1)
    assert months.start_from_name("scada_summaries_default") is None
    assert (samples.retention, months.retention) == (timedelta(days=90), timedelta(days=730))


def test_retention_comes_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINDOPS_TELEMETRY_RAW_RETENTION_DAYS", "30")
    monkeypatch.setenv("WINDOPS_TELEMETRY_SUMMARY_RETENTION_DAYS", "365")
    samples, months = partitions.tables()
    assert (samples.retention, months.retention) == (timedelta(days=30), timedelta(days=365))


def test_old_rejections_are_deleted_with_the_raw_samples_retention() -> None:
    async def scenario(engine: AsyncEngine) -> None:
        now = START
        async with engine.begin() as connection:
            await connection.execute(
                INGEST_REJECTIONS.insert(),
                [
                    {"tenant_id": "north-sea", "rejection_id": name, "device_id": "dev_1",
                     "turbine_id": TURBINE, "received_at": now - age, "reason": "not_json",
                     "excerpt": "{"}
                    for name, age in (("old", timedelta(days=91)), ("new", timedelta(days=89)))
                ],
            )  # fmt: skip
        assert await partitions.expire(engine, now) == []
        assert await partitions.ensure(engine) == []
        async with engine.connect() as connection:
            left = (await connection.execute(select(INGEST_REJECTIONS.c.rejection_id))).scalars()
            assert list(left) == ["new"]

    with_database(scenario)
