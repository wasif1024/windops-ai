"""10-minute SCADA summaries, kept up to date as samples arrive, late ones too (FR-TEL-004).

Every minute, the summarizer reads which samples arrived since it last looked, by ingestion time,
and finds the 10-minute periods they belong to. Each closed period among them is summarized again
from all its samples, so a late or out-of-order sample changes its period's summary and raises
its revision (SRS 8.3). A period still open is left for a later run: the watermark stays at its
first new sample, so it is found again once it closes.

Duplicates, which share a turbine and sequence, are counted once. Statistics skip null readings.
Wind direction uses a circular mean and spread, as 350 and 10 degrees average to north, not south.
"""

import asyncio
import contextlib
import logging
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_migrations import across_tenants, in_tenant

from windops_telemetry.schema import READINGS
from windops_telemetry.store import (
    OPERATIONAL_SAMPLES,
    SCADA_SUMMARIES,
    STATISTICS,
    SUMMARY_WATERMARKS,
)

logger = logging.getLogger("windops.service")
PERIOD = timedelta(minutes=10)
EXPECTED_SAMPLES = 600
# How long after a period ends before it is first summarized, for batches still on their way.
GRACE = timedelta(seconds=60)
# On its first run, the summarizer reads back this far.
FIRST_LOOKBACK = timedelta(days=1)
RUN_SECONDS = 60
WATERMARK = "scada"
LOCK_KEY = 4_400_002
DIRECTION = "wind_direction_deg"
COMPLETE, PARTIAL = 0.95, 0.5

Period = tuple[str, str, datetime]


def period_start(moment: datetime) -> datetime:
    seconds = int(moment.timestamp())
    return datetime.fromtimestamp(seconds - seconds % int(PERIOD.total_seconds()), UTC)


def quality_for(coverage: float) -> str:
    if coverage >= COMPLETE:
        return "complete"
    return "partial" if coverage >= PARTIAL else "sparse"


def _circular(degrees: Sequence[float]) -> tuple[float, float]:
    """The mean direction, and its spread in degrees, from the mean resultant length."""
    sines = sum(math.sin(math.radians(d)) for d in degrees) / len(degrees)
    cosines = sum(math.cos(math.radians(d)) for d in degrees) / len(degrees)
    mean = math.degrees(math.atan2(sines, cosines)) % 360
    length = min(1.0, math.hypot(sines, cosines))
    spread = math.degrees(math.sqrt(-2 * math.log(length))) if length > 0 else 180.0
    return round(mean, 3), round(spread, 3)


def _utc(value: datetime) -> datetime:
    # SQLite returns times without a zone. They were stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def summarize(
    rows: Iterable[Any], start: datetime, first_computed_at: datetime | None = None
) -> dict[str, Any]:
    """One period's summary, from its samples. A pure function, so it is easy to test."""
    by_sequence: dict[int, Any] = {}
    total = 0
    for row in sorted(rows, key=lambda r: (r.sequence, _utc(r.ingested_at))):
        total += 1
        by_sequence.setdefault(row.sequence, row)
    samples = list(by_sequence.values())
    count = len(samples)
    coverage = min(1.0, count / EXPECTED_SAMPLES)
    late = sum(_utc(s.ingested_at) > first_computed_at for s in samples) if first_computed_at else 0
    values: dict[str, Any] = {
        "period_end": start + PERIOD,
        "sample_count": count,
        "coverage": coverage,
        "quality_status": quality_for(coverage),
        "duplicate_samples": total - count,
        "late_samples": late,
        "null_readings": sum(getattr(s, r.name) is None for s in samples for r in READINGS),
        "available_fraction": sum(bool(s.available) for s in samples) / count,
        "producing_fraction": sum(s.operating_state == "producing" for s in samples) / count,
        "source": Counter(s.source for s in samples).most_common(1)[0][0],
        "schema_version": Counter(s.schema_version for s in samples).most_common(1)[0][0],
        "model_version": Counter(s.model_version for s in samples).most_common(1)[0][0],
        "synthetic": all(bool(s.synthetic) for s in samples),
    }
    for reading in READINGS:
        present = [float(v) for s in samples if (v := getattr(s, reading.name)) is not None]
        stats: dict[str, float | None] = dict.fromkeys(STATISTICS)
        if present:
            stats["min"], stats["max"] = min(present), max(present)
            if reading.name == DIRECTION:
                stats["mean"], stats["std"] = _circular(present)
            else:
                stats["mean"] = statistics.fmean(present)
                stats["std"] = statistics.pstdev(present)
        for name, value in stats.items():
            values[f"{reading.name}_{name}"] = value
    mean_power = values["power_kw_mean"]
    # The mean power over the whole period, so a gap in the data does not read as no output.
    values["energy_kwh"] = (
        None if mean_power is None else mean_power * PERIOD.total_seconds() / 3600
    )
    return values


def _insert(connection: AsyncConnection) -> Any:
    dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
    return dialect.insert(SCADA_SUMMARIES)


async def write_summary(
    connection: AsyncConnection, tenant: str, turbine: str, start: datetime, now: datetime
) -> bool:
    """Summarize one period again from all its samples. False if it has none."""
    await in_tenant(connection, tenant)
    rows = (
        await connection.execute(
            select(OPERATIONAL_SAMPLES).where(
                OPERATIONAL_SAMPLES.c.tenant_id == tenant,
                OPERATIONAL_SAMPLES.c.turbine_id == turbine,
                OPERATIONAL_SAMPLES.c.event_time >= start,
                OPERATIONAL_SAMPLES.c.event_time < start + PERIOD,
            )
        )
    ).all()
    if not rows:
        return False
    first = (
        await connection.execute(
            select(SCADA_SUMMARIES.c.first_computed_at).where(
                SCADA_SUMMARIES.c.tenant_id == tenant,
                SCADA_SUMMARIES.c.turbine_id == turbine,
                SCADA_SUMMARIES.c.period_start == start,
            )
        )
    ).scalar_one_or_none()
    # Late samples are those that arrived after the period was first summarized.
    values = summarize(rows, start, _utc(first) if first else None)
    statement = _insert(connection).values(
        tenant_id=tenant, turbine_id=turbine, period_start=start, revision=1,
        first_computed_at=now, computed_at=now, **values,
    )  # fmt: skip
    changed = {name: statement.excluded[name] for name in (*values, "computed_at")}
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["tenant_id", "turbine_id", "period_start"],
            set_={**changed, "revision": SCADA_SUMMARIES.c.revision + 1},
        )
    )
    return True


class Summarizer:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def _watermark(self, connection: AsyncConnection, now: datetime) -> datetime:
        found = (
            await connection.execute(
                select(SUMMARY_WATERMARKS.c.watermark).where(SUMMARY_WATERMARKS.c.name == WATERMARK)
            )
        ).scalar_one_or_none()
        return _utc(found) if found else now - FIRST_LOOKBACK

    async def _save(self, connection: AsyncConnection, watermark: datetime) -> None:
        dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
        statement = dialect.insert(SUMMARY_WATERMARKS).values(name=WATERMARK, watermark=watermark)
        await connection.execute(
            statement.on_conflict_do_update(
                index_elements=["name"], set_={"watermark": statement.excluded.watermark}
            )
        )

    async def run_once(self, now: datetime | None = None) -> list[Period]:
        """Summarize every closed period with new samples, and return them."""
        now = now or datetime.now(UTC)
        async with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                # One replica at a time. Another that finds the lock taken skips this run.
                taken = await connection.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_KEY}
                )
                if not taken.scalar():
                    return []
            await across_tenants(connection)
            since = await self._watermark(connection, now)
            arrived = (
                await connection.execute(
                    select(
                        OPERATIONAL_SAMPLES.c.tenant_id,
                        OPERATIONAL_SAMPLES.c.turbine_id,
                        OPERATIONAL_SAMPLES.c.event_time,
                        OPERATIONAL_SAMPLES.c.ingested_at,
                    ).where(
                        OPERATIONAL_SAMPLES.c.ingested_at > since,
                        OPERATIONAL_SAMPLES.c.ingested_at <= now,
                    )
                )
            ).all()
            closed: set[Period] = set()
            next_watermark = now
            for row in arrived:
                start = period_start(_utc(row.event_time))
                if start + PERIOD + GRACE <= now:
                    closed.add((row.tenant_id, row.turbine_id, start))
                else:
                    # Still open: read this sample again next time, so the period is found once
                    # it closes.
                    next_watermark = min(next_watermark, _utc(row.ingested_at) - timedelta(
                        microseconds=1))  # fmt: skip
            done = []
            for tenant, turbine, start in sorted(closed):
                if await write_summary(connection, tenant, turbine, start, now):
                    done.append((tenant, turbine, start))
            await across_tenants(connection)
            await self._save(connection, next_watermark)
        return done


async def keep_summarizing(engine: AsyncEngine, stop: asyncio.Event) -> None:
    summarizer = Summarizer(engine)
    while not stop.is_set():
        try:
            done = await summarizer.run_once()
            if done:
                logger.info("Wrote SCADA summaries", extra={"summaries": len(done)})
        except Exception as error:  # Retried next minute. Samples are safe meanwhile.
            logger.warning(
                "Could not write SCADA summaries", extra={"error_type": type(error).__name__}
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=RUN_SECONDS)
