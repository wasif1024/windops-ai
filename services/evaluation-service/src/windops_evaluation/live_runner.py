"""Runs the live suite, keeps each run, and runs it on a schedule (EVL-001, M12.6b).

The suite asks the platform's own services as the Evaluation Service itself (ADR 0008), in the
deployment's live tenant, with fixed read grants: a turbine, its forecast, and research. A tenant
the deployment replays from recordings is refused, as its answers would test nothing live.

- `WINDOPS_LIVE_SUITE_TENANT`: the tenant. Unset, the suite is off, and asking for a run says so.
- `WINDOPS_LIVE_SUITE_HOURS`: how often the schedule runs it. Unset or 0, only on request.

A scheduled run's ID is its time slot, so with two replicas only one runs it.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.asset.v1 import asset_pb2
from windops.evaluation.v1 import evaluation_pb2 as pb
from windops.external_intelligence.v1 import external_intelligence_pb2 as ei_pb2
from windops.weather.v1 import weather_pb2
from windops_auth.identity import VerifiedIdentity
from windops_grpc import ReplaySettings, ServiceClient, acting_as_service
from windops_migrations import in_tenant

from windops_evaluation import live
from windops_evaluation.store import LIVE_RUNS, PLATFORM_TENANT, SERVICE_NAME, audit

logger = logging.getLogger("windops.service")
RUNNING = "running"
# How long the services may take: a forecast or research may reach its provider on the way.
DEADLINES = {"asset": 3.0, "weather": 12.0, "external-intelligence": 25.0}
# The latest finished run, for the metrics: its outcome, and when it finished.
LATEST: dict[str, Any] = {"passed": None, "finished_at": None}


@dataclass(frozen=True)
class LiveSettings:
    tenant: str
    hours: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "LiveSettings | None":
        tenant = env.get("WINDOPS_LIVE_SUITE_TENANT", "").strip()
        if not tenant:
            return None
        replayed = ReplaySettings.from_env(env)
        if replayed is not None and tenant in replayed.tenants:
            raise ValueError(f"The live suite cannot run in {tenant}: its providers are recorded")
        return cls(tenant, float(env.get("WINDOPS_LIVE_SUITE_HOURS", "0") or 0))


class ServiceProviders:
    """The platform's services, asked as the Evaluation Service, in the live tenant."""

    def __init__(self, stubs: Mapping[str, Any], clients: Mapping[str, ServiceClient],
                 tenant: str, secret: str) -> None:  # fmt: skip
        self._stubs, self._clients = stubs, clients
        self._tenant, self._secret = tenant, secret

    async def _ask(self, service: str, method: str, request: Any, idempotent: bool = True) -> Any:
        with acting_as_service(SERVICE_NAME, self._tenant, self._secret):
            return await self._clients[service].call(
                getattr(self._stubs[service], method), request, idempotent=idempotent,
                deadline_seconds=DEADLINES[service],
            )  # fmt: skip

    async def a_turbine(self) -> str:
        found = await self._ask("asset", "ListTurbines", asset_pb2.ListTurbinesRequest(page_size=1))
        return str(found.turbines[0].turbine_id) if found.turbines else ""

    async def forecast(self, turbine_id: str, start: datetime, end: datetime) -> Any:
        return await self._ask("weather", "GetForecast", weather_pb2.GetForecastRequest(
            turbine_id=turbine_id, from_time=_stamp(start), to_time=_stamp(end)))  # fmt: skip

    async def research(self, query: str, topic: str, key: str) -> Any:
        return await self._ask("external-intelligence", "Research", ei_pb2.ResearchRequest(
            topic=topic, query=query, max_results=3, idempotency_key=key))  # fmt: skip

    async def extract(self, research_id: str, source_id: str, key: str) -> Any:
        return await self._ask("external-intelligence", "Extract", ei_pb2.ExtractRequest(
            research_id=research_id, source_ids=[source_id], idempotency_key=key))  # fmt: skip


def _stamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def message(row: Any) -> pb.LiveRun:
    return pb.LiveRun(
        run_id=row.run_id, live_tenant_id=row.live_tenant_id, outcome=row.outcome,
        checks=[pb.LiveCheck(**c) for c in json.loads(row.checks_json)],
        started_at=_stamp(row.started_at), finished_at=_stamp(row.finished_at),
        triggered_by=row.triggered_by,
    )  # fmt: skip


def manual_run_id(key: str) -> str:
    """A requested run's ID: from its idempotency key, so a retry finds the first run."""
    if not key:
        return f"live-{secrets.token_hex(10)}"
    return f"live-key-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


async def stored(engine: AsyncEngine, run_id: str) -> Any:
    r = LIVE_RUNS.c
    async with engine.connect() as connection:
        await in_tenant(connection, PLATFORM_TENANT)
        return (await connection.execute(
            select(LIVE_RUNS).where(r.tenant_id == PLATFORM_TENANT, r.run_id == run_id)
        )).one_or_none()  # fmt: skip


async def latest(engine: AsyncEngine, limit: int) -> Sequence[Any]:
    r = LIVE_RUNS.c
    async with engine.connect() as connection:
        await in_tenant(connection, PLATFORM_TENANT)
        return (await connection.execute(
            select(LIVE_RUNS).where(r.tenant_id == PLATFORM_TENANT)
            .order_by(r.started_at.desc()).limit(limit)
        )).all()  # fmt: skip


async def run_and_keep(
    engine: AsyncEngine, providers: live.Providers, settings: LiveSettings, run_id: str,
    triggered_by: str, now: Callable[[], datetime],
) -> Any:  # fmt: skip
    """Run the suite once under this ID, and keep it. If the ID was already taken, by a retry or
    another replica, nothing runs, and the run under it is returned."""
    started = now()
    r = LIVE_RUNS.c
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, PLATFORM_TENANT)
            await connection.execute(insert(LIVE_RUNS).values(
                tenant_id=PLATFORM_TENANT, run_id=run_id, live_tenant_id=settings.tenant,
                outcome=RUNNING, started_at=started, triggered_by=triggered_by,
            ))  # fmt: skip
    except IntegrityError:
        return await stored(engine, run_id)
    checks = await live.run(providers, started, run_id)
    result = live.outcome(checks)
    finished = now()
    who = VerifiedIdentity(subject=f"service:{SERVICE_NAME}", tenant_id=PLATFORM_TENANT)
    async with engine.begin() as connection:
        await in_tenant(connection, PLATFORM_TENANT)
        await connection.execute(
            update(LIVE_RUNS).where(r.tenant_id == PLATFORM_TENANT, r.run_id == run_id)
            .values(outcome=result, finished_at=finished,
                    checks_json=json.dumps([c.__dict__ for c in checks]))
        )  # fmt: skip
        await audit(connection, who, action=f"evaluation.live_suite.{result}",
                    resource_type="live_run", resource_id=run_id,
                    outcome="succeeded" if result == "passed" else "failed")  # fmt: skip
    LATEST.update(passed=result == "passed", finished_at=finished)
    logger.info(
        "Live suite run",
        extra={
            "run_id": run_id,
            "outcome": result,
            "failed": [c.name for c in checks if not c.passed],
        },
    )
    return await stored(engine, run_id)


async def schedule(
    engine: AsyncEngine, providers: live.Providers, settings: LiveSettings,
    stop: asyncio.Event, now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:  # fmt: skip
    """Run it once each interval, its slot the run's ID. A failure is kept, never raised."""
    # The metrics start from the latest run kept, so a restart never hides a failure or a stop.
    for row in await latest(engine, 5):
        if row.outcome != RUNNING and row.finished_at is not None:
            LATEST.update(passed=row.outcome == "passed", finished_at=row.finished_at)
            break
    logger.info("Live suite scheduled", extra={
        "live_tenant": settings.tenant, "every_hours": settings.hours,
        "latest_passed": LATEST["passed"]})  # fmt: skip
    if settings.hours <= 0:
        # Only on request. Returning would stop the service, so it waits to be stopped.
        await stop.wait()
        return
    period = settings.hours * 3600
    while not stop.is_set():
        slot = int(now().timestamp() // period)
        try:
            await run_and_keep(engine, providers, settings, f"live-slot-{slot}", "schedule", now)
        except Exception as error:  # The schedule must outlive any one run.
            logger.warning("The live suite could not run",
                           extra={"error_type": type(error).__name__})  # fmt: skip
        wait = (slot + 1) * period - now().timestamp() + 5
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(60.0, wait))
