"""Data-quality labels and gaps, scored against the simulator's ground truth (FR-TEL-003)."""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops_outbox import create_outbox_table
from windops_simulator.delivery import DataProblems
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.scenarios import Scenario, run
from windops_telemetry import credentials, quality
from windops_telemetry.ingest import PREFIX, create_ingest_router
from windops_telemetry.limits import DeviceLimiter
from windops_telemetry.store import (
    DATA_GAPS,
    DEVICE_CREDENTIALS,
    INGEST_REJECTIONS,
    METADATA,
    OPERATIONAL_SAMPLES,
    SCADA_SUMMARIES,
)
from windops_telemetry.summaries import Summarizer

START = datetime.fromisoformat("2026-01-15T06:00:00+00:00")


def sample(sequence: int, arrived: float, index: int = 0, measured: float | None = None) -> Any:
    """A stored row: measured at `sequence` seconds, arrived `arrived` seconds after START."""
    return SimpleNamespace(
        sequence=sequence,
        event_time=START + timedelta(seconds=sequence if measured is None else measured),
        ingested_at=START + timedelta(seconds=arrived),
        arrival_index=index,
    )


def labelled(rows: list[Any], context: list[Any] | None = None) -> list[str]:
    found = quality.label(rows, context or [])
    return [found[quality.key_of(row)] for row in rows]


# --- the labels


def test_samples_in_order_are_ok() -> None:
    rows = [sample(n, 5, index=n) for n in range(1, 6)]
    assert labelled(rows) == ["ok"] * 5


def test_a_second_copy_is_a_duplicate_and_the_first_stays_ok() -> None:
    rows = [sample(1, 5, 0), sample(2, 5, 1), sample(2, 5, 2), sample(3, 10, 0)]
    assert labelled(rows) == ["ok", "ok", "duplicate", "ok"]


def test_a_sample_held_back_past_the_threshold_is_delayed() -> None:
    rows = [sample(1, 5, 0), sample(2, 200, 0), sample(3, 5, 1)]
    assert labelled(rows) == ["ok", "delayed", "ok"]


def test_a_sample_sent_after_the_next_one_is_out_of_order() -> None:
    rows = [sample(1, 5, 0), sample(3, 5, 1), sample(2, 5, 2), sample(4, 10, 0)]
    assert labelled(rows) == ["ok", "ok", "out_of_order", "ok"]


def test_a_delayed_sample_does_not_make_the_next_ones_look_out_of_order() -> None:
    rows = [sample(1, 5, 0), sample(5, 100, 0), sample(2, 101, 0), sample(3, 101, 1)]
    assert labelled(rows) == ["ok", "delayed", "delayed", "delayed"]
    on_time = [sample(1, 5, 0), sample(9, 200, 0, measured=9), sample(2, 6, 0), sample(3, 7, 0)]
    assert labelled(on_time) == ["ok", "delayed", "ok", "ok"]


def test_a_neighbour_from_the_next_period_that_arrived_first_counts() -> None:
    mine = [sample(599, 600, 1)]
    neighbour = [sample(600, 600, 0)]
    assert labelled(mine, neighbour) == ["out_of_order"]


def test_gaps_are_the_runs_of_missing_sequences_with_malformed_ones_not_missing() -> None:
    assert quality.gaps([1, 2, 5, 6, 9], before=None) == [quality.Gap(3, 4), quality.Gap(7, 8)]
    assert quality.gaps([5, 6], before=1) == [quality.Gap(2, 4)]
    assert quality.gaps([1, 2, 5], before=None, malformed=[3]) == [quality.Gap(4, 4)]
    assert quality.gaps([], before=7) == []
    assert quality.Gap(3, 7).missing == 5


# --- against the ground truth


class Counts:
    async def incrby(self, name: str, amount: int) -> int:
        return 1

    async def expire(self, name: str, time: int) -> bool:
        return True


SCENARIO = Scenario(
    "quality-check", "Two healthy turbines over a poor link, for scoring labels", 2024,
    START, timedelta(hours=1),
    tuple(spec for spec in DEMO_TURBINES if spec.turbine_id in {"dogger-east-t01",
                                                                 "atlas-ridge-t01"}),
    (),
    DataProblems(missing=0.02, duplicated=0.02, delayed=0.01, malformed=0.01, out_of_order=0.02),
)  # fmt: skip
BATCH = timedelta(seconds=5)


async def replay(engine: AsyncEngine) -> Any:
    """Send the scenario through the real route, in 5-second batches, as its devices would."""
    deliveries, truth = run(SCENARIO)
    tokens = {}
    async with engine.begin() as connection:
        for spec in SCENARIO.turbines:
            device_id, secret = credentials.new_device_id(), credentials.new_secret()
            await connection.execute(
                DEVICE_CREDENTIALS.insert().values(
                    tenant_id=spec.tenant_id, device_id=device_id, turbine_id=spec.turbine_id,
                    secret_hash=credentials.hashed(secret), created_by="u", created_at=START,
                )
            )  # fmt: skip
            tokens[spec.turbine_id] = credentials.token(spec.tenant_id, device_id, secret)
    batches: dict[tuple[str, datetime], list[Any]] = defaultdict(list)
    for delivery in deliveries:
        payload = delivery.payload
        if isinstance(payload, dict):
            turbine = payload["turbine_id"]
        else:
            # Text cut off part way still starts with its tenant and turbine.
            turbine = next(spec.turbine_id for spec in SCENARIO.turbines
                           if spec.turbine_id in payload)  # fmt: skip
        seconds = (delivery.sent_at - START).total_seconds()
        window = START + BATCH * (int(seconds // BATCH.total_seconds()) + 1)
        batches[(turbine, window)].append(payload)
    clock = {"now": START}
    app = FastAPI()
    app.include_router(
        create_ingest_router(engine, DeviceLimiter(Counts()), clock=lambda: clock["now"])
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ingest"
    ) as client:
        for (turbine, window), items in sorted(batches.items(), key=lambda item: item[0][1]):
            clock["now"] = window
            await client.post(
                f"{PREFIX}/samples", json={"samples": items},
                headers={"Authorization": f"Bearer {tokens[turbine]}"},
            )  # fmt: skip
    return truth


def test_labels_match_the_ground_truth_for_every_kind_of_problem() -> None:
    """FR-TEL-003: every injected problem is labelled, and what is labelled was injected."""

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        truth = await replay(engine)
        await Summarizer(engine).run_once(START + SCENARIO.duration + timedelta(minutes=20))
        async with engine.connect() as connection:
            rows = (await connection.execute(select(OPERATIONAL_SAMPLES))).all()
            gaps = (await connection.execute(select(DATA_GAPS))).all()
            rejections = (await connection.execute(select(INGEST_REJECTIONS))).all()
            summaries = (await connection.execute(select(SCADA_SUMMARIES))).all()
        await engine.dispose()

        injected: dict[str, set[tuple[str, int]]] = defaultdict(set)
        for problem in truth.problems:
            injected[problem.kind].add((problem.turbine_id, problem.sequence))
        labels: dict[str, set[tuple[str, int]]] = defaultdict(set)
        for row in rows:
            labels[row.quality_status].add((row.turbine_id, row.sequence))
        missing = {
            (g.turbine_id, s) for g in gaps for s in range(g.first_sequence, g.last_sequence + 1)
        }
        assert "received" not in labels, "every sample is labelled"

        assert labels["duplicate"] == injected["duplicated"]
        assert labels["delayed"] == injected["delayed"]
        # Text cut off part way cannot say which sample it was, so its slot reads as missing.
        cut_off = {
            (p.turbine_id, p.sequence)
            for p in truth.problems
            if p.kind == "malformed" and p.detail == "not_json"
        }
        assert missing == injected["missing"] | cut_off
        # Held back and sent after the next sample: seen, unless that next one never arrived in
        # time, being lost, broken or delayed itself, when nothing out of order reached the wire.
        assert labels["out_of_order"] <= injected["out_of_order"]
        unseen = {key for kind in ("missing", "malformed", "delayed") for key in injected[kind]}
        for turbine, sequence in injected["out_of_order"] - labels["out_of_order"]:
            assert (turbine, sequence + 1) in unseen, (turbine, sequence)
        readable = {(p.turbine_id, p.sequence) for p in truth.problems
                    if p.kind == "malformed" and p.detail != "not_json"}  # fmt: skip
        kept = {(r.turbine_id, r.sequence) for r in rejections if r.sequence is not None}
        assert kept == readable, "each readable malformed sample is kept, by sequence"
        assert len(rejections) == len(injected["malformed"])
        assert sum(s.missing_samples or 0 for s in summaries) == len(missing)
        assert sum(s.duplicate_samples for s in summaries) == len(labels["duplicate"])
        assert all(s.delayed_samples is not None for s in summaries)

    asyncio.run(scenario())


def test_a_late_sample_that_fills_a_gap_removes_it() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
            base = {
                "tenant_id": "north-sea", "turbine_id": "t1", "device_id": "d", "source": "s",
                "schema_version": "2", "model_version": "m", "synthetic": True,
                "operating_state": "producing", "available": True, "quality_status": "received",
            }  # fmt: skip

            def at(n: int, arrived: datetime) -> dict[str, Any]:
                return {**base, "sequence": n, "event_time": START + timedelta(seconds=n),
                        "ingested_at": arrived, "arrival_index": 0}  # fmt: skip

            early = START + timedelta(minutes=10, seconds=5)
            await connection.execute(
                OPERATIONAL_SAMPLES.insert(), [at(n, early) for n in range(1, 600) if n != 300]
            )
        summarizer = Summarizer(engine)
        await summarizer.run_once(START + timedelta(minutes=12))
        async with engine.connect() as connection:
            (gap,) = (await connection.execute(select(DATA_GAPS))).all()
        assert (gap.first_sequence, gap.last_sequence) == (300, 300)
        late = START + timedelta(minutes=15)
        async with engine.begin() as connection:
            await connection.execute(OPERATIONAL_SAMPLES.insert(), [at(300, late)])
        await summarizer.run_once(late + timedelta(seconds=30))
        async with engine.connect() as connection:
            assert (await connection.execute(select(DATA_GAPS))).all() == []
            row = (
                await connection.execute(
                    select(OPERATIONAL_SAMPLES.c.quality_status).where(
                        OPERATIONAL_SAMPLES.c.sequence == 300
                    )
                )
            ).scalar_one()
        assert row == "delayed"
        await engine.dispose()

    asyncio.run(scenario())
