"""Telemetry partitions: made ahead of time, and dropped whole for retention (SRS 8.3, ADR 0003).

Samples are partitioned by day on event time, and SCADA summaries by month on their period. Each
table also has a default partition, which holds anything outside its partitions, so nothing is
lost. Every hour this:

- makes the partitions from a little behind to a few ahead. Rows the default partition already
  holds for that range are moved in first, as PostgreSQL refuses a partition whose rows sit in the
  default one.
- drops each partition wholly past its retention, unless a live retention hold overlaps it: a
  snapshot's data is kept until the snapshot is released. Rows in the default partition past
  retention, and old ingest rejections, are deleted, as they have no partition to drop.

Names and bounds come from dates, never from a request, so they are safe to put in SQL.
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_migrations import across_tenants, enable_tenant_isolation

from windops_telemetry.store import INGEST_REJECTIONS, RETENTION_HOLDS

logger = logging.getLogger("windops.service")
CHECK_SECONDS = 3600
# Any fixed number, the same in every replica.
LOCK_KEY = 4_400_001


@dataclass(frozen=True)
class Partitioned:
    table: str
    column: str
    # "day" or "month".
    unit: str
    behind: int
    ahead: int
    retention: timedelta

    @property
    def default(self) -> str:
        return f"{self.table}_default"

    def start_of(self, moment: date) -> date:
        return moment if self.unit == "day" else moment.replace(day=1)

    def after(self, start: date) -> date:
        if self.unit == "day":
            return start + timedelta(days=1)
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)

    def before(self, start: date) -> date:
        if self.unit == "day":
            return start - timedelta(days=1)
        return (start - timedelta(days=1)).replace(day=1)

    def name_for(self, start: date) -> str:
        return (
            f"{self.table}_{start:%Y%m%d}" if self.unit == "day" else f"{self.table}_{start:%Y%m}"
        )

    def start_from_name(self, name: str) -> date | None:
        suffix = name.removeprefix(f"{self.table}_")
        try:
            if self.unit == "day" and len(suffix) == 8:
                return datetime.strptime(suffix, "%Y%m%d").date()  # A date only.
            if self.unit == "month" and len(suffix) == 6:
                return datetime.strptime(suffix, "%Y%m").date()  # A date only.
        except ValueError:
            return None
        return None

    def wanted(self, today: date) -> list[date]:
        """The starts of the partitions to keep ready, from `behind` to `ahead`."""
        first = self.start_of(today)
        for _ in range(self.behind):
            first = self.before(first)
        starts, current = [], first
        for _ in range(self.behind + self.ahead + 1):
            starts.append(current)
            current = self.after(current)
        return starts


def _days(name: str, default: int) -> timedelta:
    return timedelta(days=int(os.environ.get(name, str(default))))


def tables() -> tuple[Partitioned, ...]:
    """The partitioned tables, with the retention the settings give them (SRS 8.3 by default)."""
    raw = _days("WINDOPS_TELEMETRY_RAW_RETENTION_DAYS", 90)
    summaries = _days("WINDOPS_TELEMETRY_SUMMARY_RETENTION_DAYS", 730)
    return (
        Partitioned("operational_samples", "event_time", "day", 1, 3, raw),
        Partitioned("scada_summaries", "period_start", "month", 1, 2, summaries),
    )


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def _children(connection: AsyncConnection, table: str) -> set[str]:
    return set(
        (
            await connection.execute(
                text(
                    "SELECT child.relname FROM pg_inherits "
                    "JOIN pg_class parent ON parent.oid = pg_inherits.inhparent "
                    "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
                    "WHERE parent.relname = :table"
                ),
                {"table": table},
            )
        ).scalars()
    )


async def _make(connection: AsyncConnection, spec: Partitioned, start: date) -> None:
    """Make one partition, taking in any rows the default partition holds for its range."""
    name, end = spec.name_for(start), spec.after(start)
    low, high = start.isoformat(), end.isoformat()
    # Every tenant's rows move, so this one transaction sees them all.
    await across_tenants(connection)
    await connection.execute(
        text(f"CREATE TABLE {name} (LIKE {spec.table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
    )
    where = f"{spec.column} >= '{low}' AND {spec.column} < '{high}'"
    await connection.execute(
        text(f"INSERT INTO {name} SELECT * FROM {spec.default} WHERE {where}")  # noqa: S608
    )
    await connection.execute(text(f"DELETE FROM {spec.default} WHERE {where}"))  # noqa: S608
    await connection.execute(
        text(
            f"ALTER TABLE {spec.table} ATTACH PARTITION {name} "
            f"FOR VALUES FROM ('{low}') TO ('{high}')"
        )
    )
    # A partition has its own row-level security, for anything that names it directly.
    await connection.run_sync(enable_tenant_isolation, name)


async def ensure(
    engine: AsyncEngine, today: date | None = None, specs: Sequence[Partitioned] | None = None
) -> list[str]:
    """Make the partitions that are missing, and return their names."""
    if engine.dialect.name != "postgresql":
        return []
    today = today or datetime.now(UTC).date()
    made = []
    async with engine.begin() as connection:
        # One replica at a time, so two never make the same partition. Released at commit.
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
        for spec in specs or tables():
            existing = await _children(connection, spec.table)
            for start in spec.wanted(today):
                if spec.name_for(start) not in existing:
                    await _make(connection, spec, start)
                    made.append(spec.name_for(start))
    return made


async def _held(connection: AsyncConnection, table: str, start: datetime, end: datetime) -> bool:
    """Whether a live hold, in any tenant, overlaps this range of this table."""
    found = await connection.execute(
        select(RETENTION_HOLDS.c.hold_id)
        .where(
            RETENTION_HOLDS.c.table_name == table,
            RETENTION_HOLDS.c.released_at.is_(None),
            RETENTION_HOLDS.c.starts_at < end,
            RETENTION_HOLDS.c.ends_at > start,
        )
        .limit(1)
    )
    return found.first() is not None


async def expire(
    engine: AsyncEngine, now: datetime | None = None, specs: Sequence[Partitioned] | None = None
) -> list[str]:
    """Drop the partitions past their retention, unless held, and return their names."""
    now = now or datetime.now(UTC)
    dropped: list[str] = []
    async with engine.begin() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
        await across_tenants(connection)
        raw = next(spec for spec in specs or tables() if spec.table == "operational_samples")
        # Rejections are not partitioned, so they go row by row, with the raw samples' retention.
        await connection.execute(
            delete(INGEST_REJECTIONS).where(INGEST_REJECTIONS.c.received_at < now - raw.retention)
        )
        if not postgres:
            return dropped
        for spec in specs or tables():
            cutoff = now - spec.retention
            for name in sorted(await _children(connection, spec.table)):
                start = spec.start_from_name(name)
                if start is None:
                    continue
                end = _midnight(spec.after(start))
                if end > cutoff:
                    continue
                if await _held(connection, spec.table, _midnight(start), end):
                    logger.info("Kept a partition past retention, for a hold",
                                extra={"partition": name})  # fmt: skip
                    continue
                await connection.execute(text(f"DROP TABLE {name}"))
                dropped.append(name)
            held = await connection.execute(
                select(RETENTION_HOLDS.c.starts_at).where(
                    RETENTION_HOLDS.c.table_name == spec.table,
                    RETENTION_HOLDS.c.released_at.is_(None),
                    RETENTION_HOLDS.c.starts_at < cutoff,
                )
            )
            earliest = min((row.starts_at for row in held), default=cutoff)
            await connection.execute(
                text(f"DELETE FROM {spec.default} WHERE {spec.column} < :cutoff"),  # noqa: S608
                {"cutoff": min(cutoff, earliest)},
            )
    return dropped


async def keep_ahead(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Every hour: make the partitions ahead, and drop those past retention."""
    while not stop.is_set():
        try:
            made = await ensure(engine)
            if made:
                logger.info("Made telemetry partitions", extra={"partitions": made})
            dropped = await expire(engine)
            if dropped:
                logger.info("Dropped telemetry partitions past retention",
                            extra={"partitions": dropped})  # fmt: skip
        # A failed check is retried, and the default partition holds data meanwhile.
        except Exception as error:
            logger.warning(
                "Could not keep telemetry partitions", extra={"error_type": type(error).__name__}
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=CHECK_SECONDS)
