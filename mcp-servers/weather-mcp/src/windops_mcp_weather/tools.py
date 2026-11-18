"""The Weather MCP's tool: a turbine's structured forecast over a window, with its provenance.

It calls the Weather Service as the verified caller, which needs `weather.read` for the turbine.
Values come only from the structured provider, each only when the provider gave it, with the
provider, licence, retrieval time and age (WTH-003, WTH-005). A stale or unavailable forecast says
so: it is never shown as current. The answer is numbers and provenance, with no provider text, so
nothing in it needs wrapping.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import AwareDatetime, Field, model_validator
from windops.weather.v1 import weather_pb2, weather_pb2_grpc
from windops_mcp import Tool, ToolInput, ToolOutput
from windops_mcp.serve import Services

SERVER = "weather"
INSTRUCTIONS = (
    "Read a turbine's structured weather forecast for a time window, with the provider, its "
    "licence, when it was retrieved and how old it is. A stale or unavailable forecast says so."
)
_ADDRESS = "WINDOPS_WEATHER_ADDRESS"
MAXIMUM_WINDOW = timedelta(days=7)


class GetForecast(ToolInput):
    turbine_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    from_time: AwareDatetime = Field(description="Start of the window, with its time zone")
    to_time: AwareDatetime = Field(description="End of the window, with its time zone")

    @model_validator(mode="after")
    def _window(self) -> "GetForecast":
        if not timedelta(0) < self.to_time - self.from_time <= MAXIMUM_WINDOW:
            raise ValueError("The window ends after it starts, and spans at most 7 days")
        return self


class Point(ToolOutput):
    valid_at: str
    wind_speed_10m_mps: float | None
    wind_speed_hub_mps: float | None
    wind_gusts_10m_mps: float | None
    wind_direction_10m_deg: float | None
    temperature_2m_c: float | None
    precipitation_mm: float | None
    visibility_m: float | None
    cape_j_per_kg: float | None


class Summary(ToolOutput):
    max_wind_speed_hub_mps: float | None
    max_wind_gusts_10m_mps: float | None
    max_wind_speed_10m_mps: float | None
    total_precipitation_mm: float | None
    min_visibility_m: float | None
    max_cape_j_per_kg: float | None
    hours_covered: int
    hours_wanted: int


class Provenance(ToolOutput):
    forecast_id: str
    provider: str
    model: str
    revision: str
    licence: str
    retrieved_at: str | None
    valid_from: str | None
    valid_to: str | None
    latitude: float
    longitude: float
    hub_height_m: float
    content_hash: str


class Forecast(ToolOutput):
    turbine_id: str
    wind_farm_id: str
    # "fresh", "stale" or "unavailable", as the Weather Service judges it.
    state: str
    note: str
    age_seconds: int
    provenance: Provenance | None
    summary: Summary | None
    points: list[Point]


def _stamp(moment: datetime) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(moment.astimezone(UTC))
    return stamp


def _time(message: Any, name: str) -> str | None:
    if not message.HasField(name):
        return None
    moment: datetime = getattr(message, name).ToDatetime(tzinfo=UTC)
    return moment.isoformat()


def _optional(message: Any, name: str) -> float | None:
    return float(getattr(message, name)) if message.HasField(name) else None


_POINT = [name for name in Point.model_fields if name != "valid_at"]
_SUMMARY = [name for name in Summary.model_fields if not name.startswith("hours_")]


def forecast(answer: Any) -> Forecast:
    found = answer.forecast if answer.HasField("forecast") else None
    return Forecast(
        turbine_id=answer.turbine_id,
        wind_farm_id=answer.wind_farm_id,
        state=answer.state,
        note=answer.note,
        age_seconds=answer.age_seconds,
        provenance=Provenance(
            forecast_id=found.forecast_id,
            provider=found.provider,
            model=found.model,
            revision=found.revision,
            licence=found.licence,
            retrieved_at=_time(found, "retrieved_at"),
            valid_from=_time(found, "valid_from"),
            valid_to=_time(found, "valid_to"),
            latitude=found.latitude,
            longitude=found.longitude,
            hub_height_m=found.hub_height_m,
            content_hash=found.content_hash,
        )
        if found is not None
        else None,
        summary=Summary(
            **{name: _optional(answer.summary, name) for name in _SUMMARY},
            hours_covered=answer.summary.hours_covered,
            hours_wanted=answer.summary.hours_wanted,
        )
        if answer.HasField("summary")
        else None,
        points=[
            Point(
                valid_at=_time(point, "valid_at") or "",
                **{name: _optional(point, name) for name in _POINT},
            )
            for point in answer.points
        ],
    )


def tools(services: Services) -> list[Tool]:
    async def get(arguments: GetForecast) -> Forecast:
        answer = await services.call(
            "weather", weather_pb2_grpc.WeatherServiceStub, _ADDRESS, "GetForecast",
            weather_pb2.GetForecastRequest(
                turbine_id=arguments.turbine_id, from_time=_stamp(arguments.from_time),
                to_time=_stamp(arguments.to_time),
            ),
            deadline_seconds=12.0,
        )  # fmt: skip
        return forecast(answer)

    return [
        Tool(
            server=SERVER,
            name="get_forecast",
            title="Get a forecast",
            description=(
                "A turbine's hourly forecast over a window of up to 7 days: wind at 10 m and at "
                "hub height, gusts, direction, temperature, precipitation, visibility and CAPE, "
                "each only when the provider gave it, with a summary and the forecast's "
                "provenance and age."
            ),
            input=GetForecast,
            output=Forecast,
            handler=get,
            timeout_seconds=15,
            per_minute=30,
            per_day=2000,
        ),
    ]
