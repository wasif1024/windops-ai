"""The recorded providers, over HTTPS for callers with a certificate from the cluster's own CA:
External Intelligence and Weather, for the evaluation tenant only (EVL-001, M12.6).

Each answer is the baseline's, or the recording of the scenario a running job's runner set,
while its lease holds. A lapsed lease gives the baseline again, so a runner that stops cannot
leave a recording in place.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from windops_migrations import in_tenant

from windops_evaluation import replay
from windops_evaluation.store import JOBS, PLATFORM_TENANT, RUNNING, SCENARIOS


async def active(engine: AsyncEngine, now: datetime) -> replay.Recorded | None:
    """The recording a running job's runner set, while its lease holds, or None."""
    j, s = JOBS.c, SCENARIOS.c
    async with engine.connect() as connection:
        await in_tenant(connection, PLATFORM_TENANT)
        found = (await connection.execute(
            select(s.definition_json)
            .join(SCENARIOS, (s.tenant_id == j.tenant_id) & (s.dataset_name == j.dataset_name)
                  & (s.dataset_version == j.dataset_version)
                  & (s.scenario_id == j.replay_scenario))
            .select_from(JOBS)
            .where(j.tenant_id == PLATFORM_TENANT, j.status == RUNNING, j.replay_scenario != "",
                   j.lease_expires_at > now)
            .order_by(j.started_at.desc())
            .limit(1)
        )).scalar_one_or_none()  # fmt: skip
    return replay.recorded(json.loads(found)) if found else None


def router(
    engine: AsyncEngine, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> APIRouter:
    routes = APIRouter(prefix="/replay")

    @routes.post("/tavily/search")
    async def search(body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        found = await active(engine, clock())
        return replay.search(str(body.get("query", "")), int(body.get("max_results") or 5), found)

    @routes.post("/tavily/extract")
    async def extract(body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        urls = [u for u in body.get("urls", []) if isinstance(u, str)][:20]
        return replay.extract(urls, await active(engine, clock()))

    @routes.get("/open-meteo/v1/forecast")
    async def forecast(
        latitude: float = Query(ge=-90, le=90),
        longitude: float = Query(ge=-180, le=180),
        past_days: int = Query(default=1, ge=0, le=7),
        forecast_days: int = Query(default=7, ge=1, le=16),
    ) -> dict[str, Any]:
        now = clock()
        return replay.forecast(latitude, longitude, past_days, forecast_days, now,
                               await active(engine, now))  # fmt: skip

    return routes
