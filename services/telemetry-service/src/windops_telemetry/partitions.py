"""Daily partitions of operational_samples, made ahead of time (SRS 8.3, ADR 0003).

The migration makes the partitioned table and a default partition, which holds any sample whose
day has no partition yet, so nothing is lost. This keeps a partition for each day from yesterday to
a few days ahead. Dropping old ones, for retention, arrives with M4.5.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops_migrations import enable_tenant_isolation

logger = logging.getLogger("windops.service")
DAYS_BEHIND, DAYS_AHEAD = 1, 3
CHECK_SECONDS = 3600
# Any fixed number, the same in every replica.
LOCK_KEY = 4_400_001


def name_for(day: date) -> str:
    return f"operational_samples_{day:%Y%m%d}"


async def ensure(engine: AsyncEngine, today: date | None = None) -> list[str]:
    """Make the partitions that are missing, and return their names."""
    if engine.dialect.name != "postgresql":
        return []
    today = today or datetime.now(UTC).date()
    made = []
    async with engine.begin() as connection:
        # One replica at a time, so two never make the same partition. Released at commit.
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
        existing = set(
            (
                await connection.execute(
                    text(
                        "SELECT child.relname FROM pg_inherits "
                        "JOIN pg_class parent ON parent.oid = pg_inherits.inhparent "
                        "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
                        "WHERE parent.relname = 'operational_samples'"
                    )
                )
            ).scalars()
        )
        for offset in range(-DAYS_BEHIND, DAYS_AHEAD + 1):
            day = today + timedelta(days=offset)
            name = name_for(day)
            if name in existing:
                continue
            # The names and bounds come from dates, never from a request, so this is safe as text.
            await connection.execute(
                text(
                    f"CREATE TABLE {name} PARTITION OF operational_samples "
                    f"FOR VALUES FROM ('{day.isoformat()}') "
                    f"TO ('{(day + timedelta(days=1)).isoformat()}')"
                )
            )
            # A partition has its own row-level security, for anything that names it directly.
            await connection.run_sync(enable_tenant_isolation, name)
            made.append(name)
    return made


async def keep_ahead(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Check every hour, so a partition is always ready before its day begins."""
    while not stop.is_set():
        try:
            made = await ensure(engine)
            if made:
                logger.info("Made telemetry partitions", extra={"partitions": made})
        # A failed check is retried, and the default partition holds data meanwhile.
        except Exception as error:
            logger.warning(
                "Could not make telemetry partitions", extra={"error_type": type(error).__name__}
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=CHECK_SECONDS)
