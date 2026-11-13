"""The Phase 4 gate's fault stream, through the real device route and summarizer (M4.10).

The gate runs it live on the cluster, in real time. Here it runs in seconds against SQLite, so a
change to the simulator, its seed or the labelling rules that would fail the gate fails here first.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from windops_outbox import create_outbox_table
from windops_simulator.scenarios import run
from windops_telemetry import credentials
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

from scripts.checks.fleet.fault_stream import TENANT, TURBINE, Stored, batches, compare, scenario

# Partway through a 10-minute period, as a live run almost always starts.
START = datetime(2026, 11, 12, 20, 7, 30, tzinfo=UTC)


class Counts:
    async def incrby(self, name: str, amount: int) -> int:
        return 1

    async def expire(self, name: str, time: int) -> bool:
        return True


async def replay() -> tuple[Any, list[Any], Stored]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(METADATA.create_all)
        await connection.run_sync(create_outbox_table)
        device_id, secret = credentials.new_device_id(), credentials.new_secret()
        await connection.execute(
            DEVICE_CREDENTIALS.insert().values(
                tenant_id=TENANT, device_id=device_id, turbine_id=TURBINE,
                secret_hash=credentials.hashed(secret), created_by="gate", created_at=START,
            )
        )  # fmt: skip
    token = credentials.token(TENANT, device_id, secret)
    planned = scenario(START)
    deliveries, truth = run(planned)
    delivered = list(deliveries)
    clock = {"now": START}
    app = FastAPI()
    app.include_router(
        create_ingest_router(engine, DeviceLimiter(Counts()), clock=lambda: clock["now"])
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://ingest") as client:
        for due, items in batches(delivered, START):
            clock["now"] = due
            answer = await client.post(
                f"{PREFIX}/samples", json={"samples": items},
                headers={"Authorization": f"Bearer {token}"},
            )  # fmt: skip
            assert answer.status_code == 200
    await Summarizer(engine).run_once(START + planned.duration + timedelta(minutes=20))
    samples, gaps, rejected, summaries = (
        OPERATIONAL_SAMPLES.c,
        DATA_GAPS.c,
        INGEST_REJECTIONS.c,
        SCADA_SUMMARIES.c,
    )
    stored = Stored()
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(samples.sequence, samples.quality_status, samples.gearbox_oil_temp_c).order_by(
                samples.ingested_at, samples.arrival_index
            )
        )
        stored.rows = [(int(r[0]), str(r[1]), r[2]) for r in rows]
        found = await connection.execute(select(gaps.first_sequence, gaps.last_sequence))
        stored.gaps = [(int(r[0]), int(r[1])) for r in found]
        refused = await connection.execute(select(rejected.sequence))
        stored.rejections = [None if r[0] is None else int(r[0]) for r in refused]
        sums = (
            await connection.execute(
                select(
                    func.coalesce(func.sum(summaries.missing_samples), 0),
                    func.coalesce(func.sum(summaries.duplicate_samples), 0),
                    func.coalesce(func.sum(summaries.delayed_samples), 0),
                )
            )
        ).one()
        stored.missing, stored.duplicates, stored.delayed = (int(n) for n in sums)
    await engine.dispose()
    return truth, [d.payload for d in delivered], stored


def test_the_gate_scenario_stores_every_problem_with_its_label() -> None:
    truth, sent, stored = asyncio.run(replay())
    failed = [message for passed, message in compare(truth, sent, stored) if not passed]
    assert not failed, failed


def test_the_gate_fails_when_a_label_is_wrong() -> None:
    truth, sent, stored = asyncio.run(replay())
    duplicate = next(i for i, (_, label, _) in enumerate(stored.rows) if label == "duplicate")
    sequence, _, oil = stored.rows[duplicate]
    stored.rows[duplicate] = (sequence, "ok", oil)
    failed = [message for passed, message in compare(truth, sent, stored) if not passed]
    assert any("sent twice" in message for message in failed), failed
