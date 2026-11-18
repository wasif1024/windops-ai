"""The Weather MCP: a structured forecast with its provenance, never shown as fresher than it is."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import ValidationError
from windops.weather.v1 import weather_pb2 as pb
from windops_mcp_weather.tools import GetForecast, tools

START = datetime(2026, 11, 20, 6, tzinfo=UTC)


def stamp(moment: datetime) -> Timestamp:
    found = Timestamp()
    found.FromDatetime(moment)
    return found


class Fake:
    def __init__(self, state: str = "fresh") -> None:
        self.state = state
        self.requests: list[Any] = []

    async def call(self, dependency: str, make: Any, setting: str, method: str, request: Any,
                   **_: Any) -> Any:  # fmt: skip
        assert (dependency, method) == ("weather", "GetForecast")
        self.requests.append(request)
        if self.state == "unavailable":
            return pb.GetForecastResponse(turbine_id="T01", state="unavailable",
                                          note="No forecast could be had")  # fmt: skip
        return pb.GetForecastResponse(
            turbine_id="T01", wind_farm_id="wf_dogger", state=self.state, age_seconds=12_000,
            forecast=pb.Forecast(forecast_id="fc_1", provider="open-meteo", licence="CC BY 4.0",
                                 retrieved_at=stamp(START), hub_height_m=110),
            points=[pb.ForecastPoint(valid_at=stamp(START), wind_speed_hub_mps=14.2,
                                     wind_gusts_10m_mps=18.0)],
            summary=pb.ForecastSummary(max_wind_speed_hub_mps=14.2, hours_covered=1,
                                       hours_wanted=2),
        )  # fmt: skip


def get(fake: Fake, **window: Any) -> Any:
    tool = tools(fake)[0]  # type: ignore[arg-type]
    arguments = GetForecast(turbine_id="T01", from_time=START, to_time=START + timedelta(hours=2),
                            **window)  # fmt: skip
    return asyncio.run(tool.handler(arguments))


def test_values_come_only_when_the_provider_gave_them_with_provenance() -> None:
    fake = Fake()
    found = get(fake)
    assert fake.requests[0].from_time.ToDatetime(tzinfo=UTC) == START
    point = found.points[0]
    assert (point.wind_speed_hub_mps, point.wind_gusts_10m_mps) == (14.2, 18.0)
    assert point.visibility_m is None and point.cape_j_per_kg is None
    assert found.provenance.provider == "open-meteo" and found.provenance.licence == "CC BY 4.0"
    assert (
        found.summary.max_wind_speed_hub_mps == 14.2
        and found.summary.total_precipitation_mm is None
    )


def test_a_stale_forecast_says_so_and_how_old_it_is() -> None:
    found = get(Fake("stale"))
    assert (found.state, found.age_seconds) == ("stale", 12_000)


def test_an_unavailable_forecast_has_no_values() -> None:
    found = get(Fake("unavailable"))
    assert found.state == "unavailable" and found.points == [] and found.provenance is None


@pytest.mark.parametrize(
    "window",
    [(START, START), (START, START - timedelta(hours=1)), (START, START + timedelta(days=8))],
)
def test_a_window_must_run_forward_and_be_at_most_a_week(window: tuple[datetime, datetime]) -> None:
    with pytest.raises(ValidationError):
        GetForecast(turbine_id="T01", from_time=window[0], to_time=window[1])


def test_a_time_without_its_zone_is_refused() -> None:
    with pytest.raises(ValidationError):
        GetForecast.model_validate(
            {
                "turbine_id": "T01",
                "from_time": "2026-11-20T06:00:00",
                "to_time": "2026-11-20T08:00:00",
            }
        )
