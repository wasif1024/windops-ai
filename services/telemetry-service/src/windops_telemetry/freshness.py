"""How fresh each turbine's telemetry is, for the Data Freshness dashboard and its alerts (M4.9).

Two measures, as SRS 10.3 asks for telemetry delay and stale data:

- **Delay:** how long after it was measured each accepted sample was stored. The device route
  records it as each batch arrives, by tenant, as `windops.telemetry.ingest.delay`.
- **Age:** how old each turbine's newest sample is now. A turbine that stops sending shows only
  here, as nothing arrives to measure. Each turbine with an active device credential is expected
  to send. The watcher reads each one's newest event time every few seconds, and the gauge works
  out the age when it is read, so it keeps growing while a turbine is silent.

A turbine is `live` while its newest sample is at most a minute old, `delayed` up to five minutes,
then `stale`, and `silent` when nothing came in the last day. The console uses the same steps.
The device route also counts the samples it accepts and rejects, by reason, for failed ingestion.
"""

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, MeterProvider, Observation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from windops_migrations import across_tenants

from windops_telemetry.store import DEVICE_CREDENTIALS, OPERATIONAL_SAMPLES

logger = logging.getLogger("windops.service")

LIVE_SECONDS = 60
DELAYED_SECONDS = 300
# Nothing newer than this, and a turbine is silent. Each turbine's newest sample is found through
# its (tenant, turbine, event time) index, so the lookup stays cheap however much is stored.
LOOKBACK = timedelta(days=1)
WATCH_SECONDS = 15
STATES = ("live", "delayed", "stale", "silent")
# From a well-behaved device's few seconds to one held back for an hour.
DELAY_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0)

Turbine = tuple[str, str]


def state_of(age: float | None) -> str:
    if age is None:
        return "silent"
    if age <= LIVE_SECONDS:
        return "live"
    return "delayed" if age <= DELAYED_SECONDS else "stale"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class IngestMetrics:
    """What the device route records about each batch.

    The instruments are made on first use, so after telemetry is running, as one made before
    then is lost. A device's first batch can only arrive once the service is serving.
    """

    def __init__(self, meter_provider: MeterProvider | None = None) -> None:
        self._provider = meter_provider
        self._delay: Any = None
        self._samples: Any = None

    def _start(self) -> None:
        if self._delay is not None:
            return
        provider = self._provider or metrics.get_meter_provider()
        meter = provider.get_meter("windops.telemetry")
        self._delay = meter.create_histogram(
            "windops.telemetry.ingest.delay",
            unit="s",
            description="How long after it was measured each accepted sample was stored",
            explicit_bucket_boundaries_advisory=DELAY_BUCKETS,
        )
        self._samples = meter.create_counter(
            "windops.telemetry.ingest.samples",
            description="Samples the device route accepted or rejected, by reason",
        )

    def accepted(
        self, tenant: str, kind: str, received: datetime, event_times: Iterable[datetime]
    ) -> None:
        self._start()
        count = 0
        for event_time in event_times:
            delay = (received - _utc(event_time)).total_seconds()
            self._delay.record(max(0.0, delay), {"tenant_id": tenant, "kind": kind})
            count += 1
        if count:
            self._samples.add(count, {"tenant_id": tenant, "kind": kind, "outcome": "accepted"})

    def rejected(self, tenant: str, kind: str, reasons: Iterable[str]) -> None:
        self._start()
        for reason, count in Counter(reasons).items():
            self._samples.add(
                count,
                {"tenant_id": tenant, "kind": kind, "outcome": "rejected", "reason": reason},
            )


class FreshnessWatcher:
    """Reads each expected turbine's newest sample, and exports how old it is."""

    def __init__(
        self,
        engine: AsyncEngine,
        meter_provider: MeterProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.now = clock or (lambda: datetime.now(UTC))
        # Each expected turbine's newest event time, or None when nothing came in LOOKBACK.
        self.newest: dict[Turbine, datetime | None] = {}
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("windops.telemetry")
        meter.create_observable_gauge(
            "windops.telemetry.sample_age_seconds",
            callbacks=[self._ages],
            description="How old each turbine's newest sample is, in seconds",
        )
        meter.create_observable_gauge(
            "windops.telemetry.turbines",
            callbacks=[self._states],
            description="Turbines expected to send, by how fresh their newest sample is",
        )

    def ages(self) -> dict[Turbine, float | None]:
        now = self.now()
        return {
            turbine: None if newest is None else max(0.0, (now - newest).total_seconds())
            for turbine, newest in self.newest.items()
        }

    def _ages(self, options: CallbackOptions) -> Iterable[Observation]:
        return [
            Observation(age, {"tenant_id": tenant, "turbine_id": turbine})
            for (tenant, turbine), age in self.ages().items()
            if age is not None
        ]

    def _states(self, options: CallbackOptions) -> Iterable[Observation]:
        counts: dict[str, Counter[str]] = {}
        for (tenant, _), age in self.ages().items():
            counts.setdefault(tenant, Counter())[state_of(age)] += 1
        return [
            Observation(found[state], {"tenant_id": tenant, "state": state})
            for tenant, found in counts.items()
            for state in STATES
        ]

    async def run_once(self) -> dict[Turbine, datetime | None]:
        since = self.now() - LOOKBACK
        samples, devices = OPERATIONAL_SAMPLES, DEVICE_CREDENTIALS
        newest = (
            select(func.max(samples.c.event_time))
            .where(
                samples.c.tenant_id == devices.c.tenant_id,
                samples.c.turbine_id == devices.c.turbine_id,
                samples.c.event_time > since,
            )
            .correlate(devices)
            .scalar_subquery()
        )
        query = (
            select(devices.c.tenant_id, devices.c.turbine_id, newest.label("newest"))
            .where(devices.c.revoked_at.is_(None))
            .distinct()
        )
        async with self.engine.begin() as connection:
            # Platform-wide, read only: one gauge for every tenant's turbines.
            await across_tenants(connection)
            rows = (await connection.execute(query)).all()
        self.newest = {
            (row.tenant_id, row.turbine_id): None if row.newest is None else _utc(row.newest)
            for row in rows
        }
        return self.newest

    async def keep_watching(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as error:  # Tried again shortly. The gauges keep their last view.
                logger.warning(
                    "Could not read telemetry freshness", extra={"error_type": type(error).__name__}
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=WATCH_SECONDS)
