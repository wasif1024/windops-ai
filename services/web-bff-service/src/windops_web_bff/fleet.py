"""The fleet screens' API: wind farms, turbines and their telemetry (M4.8, SRS 4.11). Read only.

It decides nothing. The Asset Service shows only the wind farms and turbines the caller may read,
and the Telemetry Service only the telemetry, each as the signed-in user. When the Telemetry
Service cannot be reached, the fleet is still shown, with `telemetry_available` false and no
readings, as an asset list is useful without them (SRS 7.7).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from fastapi.exceptions import RequestValidationError
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel
from windops.asset.v1 import asset_pb2
from windops.telemetry.v1 import telemetry_pb2

from windops_web_bff.common import ID_PATTERN, Call, time_of
from windops_web_bff.tokens import Caller

# The Telemetry Service answers for at most this many turbines a call.
LATEST_BATCH = 100
TURBINE_PAGE = 500
# A chart covers at most an hour: 3,600 points a metric at 1 Hz.
MAXIMUM_MINUTES = 60
# Each metric by the name the browser uses, such as "power" for METRIC_POWER.
METRICS = {
    name.removeprefix("METRIC_").lower(): number
    for name, number in telemetry_pb2.Metric.items()
    if number != telemetry_pb2.METRIC_UNSPECIFIED
}
METRIC_NAMES = {number: name for name, number in METRICS.items()}
QUALITY = {
    telemetry_pb2.QUALITY_GOOD: "good",
    telemetry_pb2.QUALITY_SUSPECT: "suspect",
    telemetry_pb2.QUALITY_MISSING: "missing",
}
TurbineId = Annotated[str, Path(pattern=ID_PATTERN)]


class Location(BaseModel):
    latitude: float
    longitude: float


class Reading(BaseModel):
    metric: str
    # None when the sensor sent nothing.
    value: float | None
    unit: str
    # "good", "suspect", "unchecked" (not yet checked) or "missing".
    quality: str


class Latest(BaseModel):
    event_time: str | None
    ingested_at: str | None
    # How long after it was measured it was stored.
    delay_seconds: float
    operating_state: str
    available: bool
    readings: list[Reading]


class TurbineSummary(BaseModel):
    turbine_id: str
    wind_farm_id: str
    name: str
    # "operating", "curtailed", "stopped", "maintenance", "fault" or "offline".
    status: str
    status_reason: str
    rated_power_kw: float
    # None when no sample came in the last day, or telemetry cannot be read.
    latest: Latest | None


class WindFarmSummary(BaseModel):
    wind_farm_id: str
    name: str
    country: str
    offshore: bool
    time_zone: str
    location: Location
    turbines: list[TurbineSummary]


class Fleet(BaseModel):
    wind_farms: list[WindFarmSummary]
    # False when the Telemetry Service could not be reached, so nothing has readings.
    telemetry_available: bool


class Component(BaseModel):
    component_id: str
    parent_component_id: str
    kind: str
    name: str
    manufacturer: str
    model: str
    serial_number: str
    installed_on: str
    # "in_service", "degraded", "failed" or "removed".
    status: str
    status_reason: str


class TurbineModel(BaseModel):
    manufacturer: str
    model: str
    rated_power_kw: float
    rotor_diameter_m: float
    hub_height_m: float


class TurbineDetail(BaseModel):
    turbine_id: str
    wind_farm_id: str
    name: str
    location: Location
    model: TurbineModel
    serial_number: str
    commissioned_on: str
    status: str
    status_reason: str
    status_changed_at: str | None
    components: list[Component]
    latest: Latest | None
    telemetry_available: bool


class Point(BaseModel):
    time: str
    # None when the sensor sent nothing.
    value: float | None
    # As a Reading's quality.
    quality: str


class Series(BaseModel):
    metric: str
    unit: str
    points: list[Point]


class TelemetryWindow(BaseModel):
    turbine_id: str
    from_time: str
    to_time: str
    series: list[Series]


def _stamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value)
    return result


# The data-quality label of a sample the summarizer has not looked at yet. It labels each
# 10-minute period after it ends, so the newest readings on a live chart are mostly this.
NOT_YET_CHECKED = "received"


def _quality(point: Any) -> str:
    """A reading's quality: "unchecked" for one not yet checked, so a live chart marks only the
    readings found delayed or out of order, else "good", "suspect" or "missing"."""
    if point.quality == telemetry_pb2.QUALITY_SUSPECT and point.quality_label == NOT_YET_CHECKED:
        return "unchecked"
    return QUALITY.get(point.quality, "suspect")


def _reading(point: Any) -> Reading:
    missing = point.quality == telemetry_pb2.QUALITY_MISSING
    return Reading(
        metric=METRIC_NAMES.get(point.metric, "unknown"),
        value=None if missing else point.value,
        unit=point.unit,
        quality=_quality(point),
    )


def _latest(message: Any) -> Latest:
    delay = message.ingested_at.ToDatetime() - message.event_time.ToDatetime()
    return Latest(
        event_time=time_of(message, "event_time"),
        ingested_at=time_of(message, "ingested_at"),
        delay_seconds=delay.total_seconds(),
        operating_state=message.operating_state,
        available=message.available,
        readings=[_reading(point) for point in message.points],
    )


def _location(message: Any) -> Location:
    return Location(latitude=message.latitude, longitude=message.longitude)


def add_fleet_routes(
    router: APIRouter,
    caller: Callable[..., Awaitable[Caller]],
    call_as: Call,
    unavailable: Callable[[Exception], bool],
) -> None:
    """`unavailable` says whether an error means the Telemetry Service could not be reached."""

    async def latest_for(turbines: list[str], who: Caller) -> tuple[dict[str, Latest], bool]:
        found: dict[str, Latest] = {}
        try:
            for start in range(0, len(turbines), LATEST_BATCH):
                answer = await call_as(
                    "telemetry", "GetLatestTelemetry",
                    telemetry_pb2.GetLatestTelemetryRequest(
                        turbine_ids=turbines[start : start + LATEST_BATCH]
                    ),
                    who,
                )  # fmt: skip
                found |= {item.turbine_id: _latest(item) for item in answer.turbines}
        except Exception as error:
            if not unavailable(error):
                raise
            return {}, False
        return found, True

    @router.get(
        "/fleet",
        response_model=Fleet,
        summary="The wind farms and turbines the user may read, each with its latest readings",
    )
    async def fleet(who: Annotated[Caller, Depends(caller)]) -> Fleet:
        farms = await call_as("asset", "ListWindFarms", asset_pb2.ListWindFarmsRequest(), who)
        turbines: list[Any] = []
        page_token = ""
        while True:
            page = await call_as(
                "asset", "ListTurbines",
                asset_pb2.ListTurbinesRequest(page_size=TURBINE_PAGE, page_token=page_token), who,
            )  # fmt: skip
            turbines.extend(page.turbines)
            page_token = page.next_page_token
            if not page_token:
                break
        latest, available = (
            await latest_for([t.turbine_id for t in turbines], who) if turbines else ({}, True)
        )
        by_farm: dict[str, list[TurbineSummary]] = {}
        for turbine in turbines:
            by_farm.setdefault(turbine.wind_farm_id, []).append(
                TurbineSummary(
                    turbine_id=turbine.turbine_id,
                    wind_farm_id=turbine.wind_farm_id,
                    name=turbine.name,
                    status=turbine.status,
                    status_reason=turbine.status_reason,
                    rated_power_kw=turbine.model.rated_power_kw,
                    latest=latest.get(turbine.turbine_id),
                )
            )
        return Fleet(
            wind_farms=[
                WindFarmSummary(
                    wind_farm_id=farm.wind_farm_id,
                    name=farm.name,
                    country=farm.country,
                    offshore=farm.offshore,
                    time_zone=farm.time_zone,
                    location=_location(farm.location),
                    turbines=by_farm.get(farm.wind_farm_id, []),
                )
                for farm in farms.wind_farms
            ],
            telemetry_available=available,
        )

    @router.get(
        "/fleet/turbines/{turbine_id}",
        response_model=TurbineDetail,
        summary="One turbine: its model, components, status and latest readings",
    )
    async def turbine(
        turbine_id: TurbineId, who: Annotated[Caller, Depends(caller)]
    ) -> TurbineDetail:
        answer = await call_as(
            "asset", "GetTurbine", asset_pb2.GetTurbineRequest(turbine_id=turbine_id), who
        )
        found, available = await latest_for([turbine_id], who)
        item, model = answer.turbine, answer.turbine.model
        return TurbineDetail(
            turbine_id=item.turbine_id,
            wind_farm_id=item.wind_farm_id,
            name=item.name,
            location=_location(item.location),
            model=TurbineModel(
                manufacturer=model.manufacturer,
                model=model.model,
                rated_power_kw=model.rated_power_kw,
                rotor_diameter_m=model.rotor_diameter_m,
                hub_height_m=model.hub_height_m,
            ),
            serial_number=item.serial_number,
            commissioned_on=item.commissioned_on,
            status=item.status,
            status_reason=item.status_reason,
            status_changed_at=time_of(item, "status_changed_at"),
            components=[
                Component(
                    component_id=c.component_id,
                    parent_component_id=c.parent_component_id,
                    kind=c.kind,
                    name=c.name,
                    manufacturer=c.manufacturer,
                    model=c.model,
                    serial_number=c.serial_number,
                    installed_on=c.installed_on,
                    status=c.status,
                    status_reason=c.status_reason,
                )
                for c in answer.components
            ],
            latest=found.get(turbine_id),
            telemetry_available=available,
        )

    @router.get(
        "/fleet/turbines/{turbine_id}/telemetry",
        response_model=TelemetryWindow,
        summary="One turbine's telemetry for the last minutes, for its charts",
    )
    async def telemetry(
        turbine_id: TurbineId,
        who: Annotated[Caller, Depends(caller)],
        minutes: Annotated[int, Query(ge=1, le=MAXIMUM_MINUTES)] = 15,
        metric: Annotated[list[str] | None, Query(max_length=len(METRICS))] = None,
    ) -> TelemetryWindow:
        names = metric or ["power", "wind_speed"]
        unknown = [name for name in names if name not in METRICS]
        if unknown:
            raise RequestValidationError([{"loc": ("query", "metric"), "msg": "Unknown metric"}])
        end = datetime.now(UTC).replace(microsecond=0)
        start = end - timedelta(minutes=minutes)
        points = await call_as(
            "telemetry", "StreamTurbineTelemetry",
            telemetry_pb2.StreamTurbineTelemetryRequest(
                turbine_id=turbine_id, from_time=_stamp(start), to_time=_stamp(end),
                metrics=[METRICS[name] for name in names],
            ),
            who,
        )  # fmt: skip
        series: dict[str, Series] = {
            name: Series(metric=name, unit="", points=[]) for name in names
        }
        for point in points:
            reading = _reading(point)
            entry = series[reading.metric]
            entry.unit = reading.unit
            entry.points.append(
                Point(time=time_of(point, "event_time") or "", value=reading.value,
                      quality=reading.quality)
            )  # fmt: skip
        return TelemetryWindow(
            turbine_id=turbine_id,
            from_time=start.isoformat().replace("+00:00", "Z"),
            to_time=end.isoformat().replace("+00:00", "Z"),
            series=list(series.values()),
        )
