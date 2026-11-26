"""Production impact and the weather, for the console's impact view and weather panel (M11.3).

It decides nothing. The Production Impact Service works out every figure, deterministically, with
a range for each and the inputs and assumptions it rests on (FR-IMP-001 to FR-IMP-003), and checks
who may estimate for a case's turbine. The Weather Service says how fresh its forecast is, and an
outage is reported, not hidden (WTH-005). Each call is made as the signed-in user.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel, Field
from windops.production_impact.v1 import production_impact_pb2 as imp
from windops.weather.v1 import weather_pb2 as wth

from windops_web_bff.common import ID_PATTERN, Call, Key, time_of
from windops_web_bff.tokens import Caller

ACTIONS = r"^(inspection|repair|replacement|adjustment|monitoring)$"


class ImpactInput(BaseModel):
    name: str
    # The value, as its JSON.
    value: Any
    # "request", "asset", "telemetry", "weather" or "reference".
    source: str
    source_ref: str
    observed_at: str | None


class Figure(BaseModel):
    name: str
    value: float
    low: float
    high: float
    unit: str


class ImpactResult(BaseModel):
    result_id: str
    case_id: str
    turbine_id: str
    wind_farm_id: str
    action: str
    downtime_hours: float
    formula_version: str
    inputs: list[ImpactInput]
    figures: list[Figure]
    assumptions: list[str]
    window_start: str | None
    window_end: str | None
    content_hash: str
    # Its hash checked again as it is read.
    verified: bool
    estimated_by: str
    estimated_at: str | None


class EstimateIn(BaseModel):
    case_id: str = Field(pattern=ID_PATTERN)
    action: str = Field(pattern=ACTIONS)
    downtime_hours: float = Field(gt=0, le=720)
    # When the downtime starts: with it, the lost energy follows the forecast hour by hour.
    planned_start: datetime | None = None


def _value(text: str) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


def _result(r: Any) -> ImpactResult:
    return ImpactResult(
        result_id=r.result_id, case_id=r.case_id, turbine_id=r.turbine_id,
        wind_farm_id=r.wind_farm_id, action=r.action, downtime_hours=r.downtime_hours,
        formula_version=r.formula_version,
        inputs=[ImpactInput(name=i.name, value=_value(i.value_json), source=i.source,
                            source_ref=i.source_ref, observed_at=time_of(i, "observed_at"))
                for i in r.inputs],
        figures=[Figure(name=f.name, value=f.value, low=f.low, high=f.high, unit=f.unit)
                 for f in r.figures],
        assumptions=list(r.assumptions), window_start=time_of(r, "window_start"),
        window_end=time_of(r, "window_end"), content_hash=r.content_hash, verified=r.verified,
        estimated_by=r.estimated_by, estimated_at=time_of(r, "estimated_at"),
    )  # fmt: skip


class ForecastPoint(BaseModel):
    valid_at: str | None
    wind_speed_hub_mps: float | None
    wind_gusts_10m_mps: float | None
    wind_speed_10m_mps: float | None
    wind_direction_10m_deg: float | None
    temperature_2m_c: float | None
    precipitation_mm: float | None
    visibility_m: float | None
    cape_j_per_kg: float | None


class ForecastSummary(BaseModel):
    max_wind_speed_hub_mps: float | None
    max_wind_gusts_10m_mps: float | None
    total_precipitation_mm: float | None
    min_visibility_m: float | None
    max_cape_j_per_kg: float | None
    hours_covered: int
    hours_wanted: int


class Forecast(BaseModel):
    turbine_id: str
    wind_farm_id: str
    # "fresh", "stale" or "unavailable" (WTH-005).
    state: str
    note: str
    # How old the forecast is, from its retrieval. 0 when there is none.
    age_seconds: int
    provider: str
    model: str
    revision: str
    forecast_id: str
    retrieved_at: str | None
    licence: str
    from_time: str | None
    to_time: str | None
    summary: ForecastSummary
    points: list[ForecastPoint]


def _optional(message: Any, name: str) -> float | None:
    return float(getattr(message, name)) if message.HasField(name) else None


def _forecast(f: Any) -> Forecast:
    source, s = f.forecast, f.summary
    return Forecast(
        turbine_id=f.turbine_id, wind_farm_id=f.wind_farm_id, state=f.state, note=f.note,
        age_seconds=f.age_seconds, provider=source.provider, model=source.model,
        revision=source.revision, forecast_id=source.forecast_id,
        retrieved_at=time_of(source, "retrieved_at"), licence=source.licence,
        from_time=time_of(f, "from_time"), to_time=time_of(f, "to_time"),
        summary=ForecastSummary(
            max_wind_speed_hub_mps=_optional(s, "max_wind_speed_hub_mps"),
            max_wind_gusts_10m_mps=_optional(s, "max_wind_gusts_10m_mps"),
            total_precipitation_mm=_optional(s, "total_precipitation_mm"),
            min_visibility_m=_optional(s, "min_visibility_m"),
            max_cape_j_per_kg=_optional(s, "max_cape_j_per_kg"),
            hours_covered=s.hours_covered, hours_wanted=s.hours_wanted,
        ),
        points=[ForecastPoint(
            valid_at=time_of(p, "valid_at"),
            **{name: _optional(p, name) for name in (
                "wind_speed_hub_mps", "wind_gusts_10m_mps", "wind_speed_10m_mps",
                "wind_direction_10m_deg", "temperature_2m_c", "precipitation_mm", "visibility_m",
                "cape_j_per_kg")},
        ) for p in f.points],
    )  # fmt: skip


def _stamp(value: datetime) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def add_impact_routes(
    router: APIRouter, caller: Callable[..., Awaitable[Caller]], call_as: Call
) -> None:
    @router.get(
        "/impact", response_model=list[ImpactResult],
        summary="A case's impact estimates, newest first",
    )  # fmt: skip
    async def results(
        case_id: Annotated[str, Query(pattern=ID_PATTERN)],
        who: Annotated[Caller, Depends(caller)],
    ) -> list[ImpactResult]:  # fmt: skip
        request = imp.ListImpactResultsRequest(case_id=case_id, page_size=50)
        answer = await call_as("production_impact", "ListImpactResults", request, who)
        return [_result(r) for r in answer.results]

    @router.get(
        "/impact/{result_id}", response_model=ImpactResult,
        summary="One estimate, its hash checked again",
    )  # fmt: skip
    async def result(
        result_id: Annotated[str, Path(pattern=ID_PATTERN)],
        who: Annotated[Caller, Depends(caller)],
    ) -> ImpactResult:  # fmt: skip
        request = imp.GetImpactResultRequest(result_id=result_id)
        answer = await call_as("production_impact", "GetImpactResult", request, who)
        return _result(answer.result)

    @router.post(
        "/impact", response_model=ImpactResult, status_code=201,
        summary="Estimate an action's impact on a case's turbine: every figure worked out, with "
        "its range, never judged",
    )  # fmt: skip
    async def estimate(
        body: EstimateIn, key: Key, who: Annotated[Caller, Depends(caller)]
    ) -> ImpactResult:
        request = imp.EstimateImpactRequest(
            case_id=body.case_id, action=body.action, downtime_hours=body.downtime_hours,
            idempotency_key=key,
            planned_start=_stamp(body.planned_start) if body.planned_start else None,
        )  # fmt: skip
        answer = await call_as("production_impact", "EstimateImpact", request, who, retry=False)
        return _result(answer.result)

    @router.get(
        "/weather/{turbine_id}", response_model=Forecast,
        summary="The forecast for a turbine's site, and how fresh it is (WTH-005)",
    )  # fmt: skip
    async def forecast(
        turbine_id: Annotated[str, Path(pattern=ID_PATTERN)],
        who: Annotated[Caller, Depends(caller)],
        hours: Annotated[int, Query(ge=1, le=168, description="From now, at most 7 days")] = 24,
    ) -> Forecast:  # fmt: skip
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        request = wth.GetForecastRequest(turbine_id=turbine_id, from_time=_stamp(now),
                                         to_time=_stamp(now + timedelta(hours=hours)))  # fmt: skip
        return _forecast(await call_as("weather", "GetForecast", request, who))
