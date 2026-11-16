"""The gRPC API: a turbine's forecast over a window, fresh, stale or unavailable (WTH-001 to 005).

A forecast is read from the cache while it is fresh, otherwise retrieved from the provider and
kept. When the provider cannot be reached, the newest kept forecast comes back with its age, and
marked stale once it is older than FRESH_FOR; with none, the answer is unavailable. Nothing old is
ever presented as current (WTH-005).
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.authorization.v1 import authorization_pb2
from windops.weather.v1 import weather_pb2 as pb
from windops.weather.v1 import weather_pb2_grpc
from windops_grpc import (
    InvalidRequestError,
    NotFoundError,
    UnavailableError,
    acting_as_service,
    current_identity,
)
from windops_migrations import in_tenant

from windops_weather import provider, sources
from windops_weather.store import FORECASTS, SERVICE_NAME

READ = "weather.read"
# The provider updates hourly: a kept forecast is used for an hour before asking again.
REFRESH_AFTER = timedelta(hours=1)
# Older than this, a forecast is stale, whatever else is known. Policy's limit is the same.
FRESH_FOR = timedelta(hours=3)
DEFAULT_SPAN = timedelta(hours=24)
FURTHEST_AHEAD = timedelta(days=provider.FORECAST_DAYS)
FURTHEST_BACK = timedelta(days=provider.PAST_DAYS)
FRESH, STALE, UNAVAILABLE = "fresh", "stale", "unavailable"
# Each normalized value, by the provider's name.
FIELDS = {
    "wind_speed_10m": "wind_speed_10m_mps", "wind_gusts_10m": "wind_gusts_10m_mps",
    "wind_direction_10m": "wind_direction_10m_deg", "temperature_2m": "temperature_2m_c",
    "precipitation": "precipitation_mm", "visibility": "visibility_m", "cape": "cape_j_per_kg",
}  # fmt: skip


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...


class Provider(Protocol):
    async def fetch(
        self, latitude: float, longitude: float, now: datetime
    ) -> provider.Retrieval: ...


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: Any) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        try:
            answer = await sources.ask(self._client, self._stub.Check, request, "Authorization")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)


def _stamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return result


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _hours_json(retrieval: provider.Retrieval) -> str:
    return json.dumps(
        [{"at": h.valid_at.isoformat(), **h.values} for h in retrieval.hours],
        sort_keys=True, separators=(",", ":"),
    )  # fmt: skip


def _point(hour: dict[str, Any], hub_height_m: float) -> pb.ForecastPoint:
    values = {k: v for k, v in hour.items() if k != "at"}
    point = pb.ForecastPoint(valid_at=_stamp(datetime.fromisoformat(hour["at"])))
    for name, field in FIELDS.items():
        if name in values:
            setattr(point, field, values[name])
    hub = provider.hub_wind(values, hub_height_m)
    if hub is not None:
        point.wind_speed_hub_mps = round(hub, 3)
    return point


def summarize(points: list[pb.ForecastPoint], wanted: int) -> pb.ForecastSummary:
    summary = pb.ForecastSummary(hours_covered=len(points), hours_wanted=wanted)

    def values(field: str) -> list[float]:
        return [getattr(p, field) for p in points if p.HasField(field)]  # type: ignore[arg-type]

    for target, field, pick in (
        ("max_wind_speed_hub_mps", "wind_speed_hub_mps", max),
        ("max_wind_gusts_10m_mps", "wind_gusts_10m_mps", max),
        ("max_wind_speed_10m_mps", "wind_speed_10m_mps", max),
        ("min_visibility_m", "visibility_m", min),
        ("max_cape_j_per_kg", "cape_j_per_kg", max),
    ):
        found = values(field)
        if found:
            setattr(summary, target, pick(found))
    rain = values("precipitation_mm")
    if rain:
        summary.total_precipitation_mm = round(sum(rain), 3)
    return summary


class WeatherService(weather_pb2_grpc.WeatherServiceServicer):
    def __init__(
        self,
        engine_: AsyncEngine,
        authorizer: Authorizer,
        sources_: sources.Sources,
        provider_: Provider,
        secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer, self._sources = engine_, authorizer, sources_
        self._provider, self._secret = provider_, secret
        self._now = clock or (lambda: datetime.now(UTC))

    def _window(self, request: pb.GetForecastRequest, now: datetime) -> tuple[datetime, datetime]:
        start = request.from_time.ToDatetime(tzinfo=UTC) if request.HasField("from_time") else now
        end = (request.to_time.ToDatetime(tzinfo=UTC) if request.HasField("to_time")
               else start + DEFAULT_SPAN)  # fmt: skip
        if end <= start:
            raise InvalidRequestError("to_time is after from_time")
        if start < now - FURTHEST_BACK or end > now + FURTHEST_AHEAD:
            raise InvalidRequestError(
                f"The window is at most {provider.FORECAST_DAYS} days ahead and "
                f"{provider.PAST_DAYS} days back"
            )
        return start, end

    async def _newest(self, tenant: str, latitude: float, longitude: float) -> Any:
        f = FORECASTS.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            return (
                await connection.execute(
                    select(FORECASTS)
                    .where(f.tenant_id == tenant, f.latitude == latitude,
                           f.longitude == longitude)
                    .order_by(f.retrieved_at.desc())
                    .limit(1)
                )
            ).one_or_none()  # fmt: skip

    async def _keep(self, tenant: str, retrieval: provider.Retrieval,
                    latitude: float, longitude: float) -> Any:  # fmt: skip
        times = [h.valid_at for h in retrieval.hours]
        values = {
            "tenant_id": tenant, "forecast_id": f"fct_{uuid.uuid4().hex[:20]}",
            "provider": provider.PROVIDER, "model": retrieval.model,
            "revision": retrieval.revision, "latitude": latitude, "longitude": longitude,
            "elevation_m": retrieval.elevation_m, "retrieved_at": retrieval.retrieved_at,
            "valid_from": min(times) if times else retrieval.retrieved_at,
            "valid_to": max(times) if times else retrieval.retrieved_at,
            "hours": _hours_json(retrieval), "content_hash": retrieval.content_hash,
        }  # fmt: skip
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await connection.execute(FORECASTS.insert().values(**values))
        return await self._newest(tenant, latitude, longitude)

    async def GetForecast(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetForecastRequest, context: Any
    ) -> pb.GetForecastResponse:
        if not request.turbine_id:
            raise InvalidRequestError("turbine_id is required")
        now = self._now()
        start, end = self._window(request, now)
        tenant = current_identity().tenant_id
        with acting_as_service(SERVICE_NAME, tenant, self._secret):
            try:
                turbine = await self._sources.turbine(request.turbine_id)
            except NotFoundError as error:
                raise NotFoundError("No such turbine") from error
        if not await self._authorizer.allowed(READ) and not await self._authorizer.allowed(
            READ, turbine.wind_farm_id, turbine.turbine_id
        ):
            # Asked about a turbine they may not read: as if it did not exist.
            raise NotFoundError("No such turbine")
        latitude, longitude = provider.rounded(turbine.location.latitude,
                                               turbine.location.longitude)  # fmt: skip
        row = await self._newest(tenant, latitude, longitude)
        note = ""
        if row is None or now - _utc(row.retrieved_at) >= REFRESH_AFTER:
            try:
                retrieval = await self._provider.fetch(turbine.location.latitude,
                                                       turbine.location.longitude, now)  # fmt: skip
                row = await self._keep(tenant, retrieval, latitude, longitude)
            except provider.ProviderUnavailableError as error:
                note = str(error)
        response = pb.GetForecastResponse(
            turbine_id=turbine.turbine_id, wind_farm_id=turbine.wind_farm_id,
            from_time=_stamp(start), to_time=_stamp(end),
        )  # fmt: skip
        if row is None:
            response.state, response.note = UNAVAILABLE, note or "No forecast has been retrieved"
            return response
        age = now - _utc(row.retrieved_at)
        response.age_seconds = int(age.total_seconds())
        if age >= FRESH_FOR:
            response.state = STALE
            response.note = (f"{note}; " if note else "") + (
                f"the newest forecast is {age.total_seconds() / 3600:.1f} h old, more than "
                f"{FRESH_FOR.total_seconds() / 3600:g} h"
            )
        else:
            response.state = FRESH
            if note:
                response.note = (
                    f"{note}; the newest forecast is {age.total_seconds() / 60:.0f} min old"
                )
        hub = turbine.model.hub_height_m
        response.forecast.CopyFrom(pb.Forecast(
            forecast_id=row.forecast_id, provider=row.provider, model=row.model,
            revision=row.revision, retrieved_at=_stamp(row.retrieved_at),
            valid_from=_stamp(row.valid_from), valid_to=_stamp(row.valid_to),
            latitude=row.latitude, longitude=row.longitude, elevation_m=row.elevation_m,
            hub_height_m=hub, content_hash=row.content_hash, licence=provider.LICENCE,
        ))  # fmt: skip
        hours = [h for h in json.loads(row.hours)
                 if start <= datetime.fromisoformat(h["at"]) < end]  # fmt: skip
        response.points.extend(_point(h, hub) for h in hours)
        wanted = max(1, int((end - start).total_seconds() // 3600))
        response.summary.CopyFrom(summarize(list(response.points), wanted))
        return response
