"""The Open-Meteo adapter against a stand-in provider: what it sends, keeps and refuses."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from windops_resilience import BreakerConfig, BreakerState, CircuitBreaker
from windops_weather import provider

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def answer(**hourly: Any) -> dict[str, Any]:
    columns = {"time": ["2026-11-16T09:00", "2026-11-16T10:00"], **hourly}
    return {"latitude": 54.8, "longitude": 1.9, "elevation": 0.0, "hourly": columns}


def adapter(handler: Any) -> tuple[provider.OpenMeteo, list[httpx.Request], CircuitBreaker]:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        result: httpx.Response = handler(request)
        return result

    breaker = CircuitBreaker("open-meteo", BreakerConfig(slow_call_seconds=5.0))
    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return provider.OpenMeteo("https://forecast.test/v1/forecast", breaker, http), sent, breaker


def test_only_rounded_coordinates_go_out_and_values_come_back_in_metres_a_second() -> None:
    body = answer(wind_speed_10m=[8.0, 9.0], wind_speed_80m=[10.0, 11.0],
                  wind_speed_120m=[12.0, 13.0], wind_gusts_10m=[14.0, None])  # fmt: skip
    meteo, sent, _ = adapter(lambda r: httpx.Response(200, json=body))
    found = asyncio.run(meteo.fetch(54.81234, 1.90678, NOW))
    query = sent[0].url.params
    assert (query["latitude"], query["longitude"], query["wind_speed_unit"]) == ("54.81", "1.91",
                                                                                "ms")  # fmt: skip
    assert found.hours[0].values["wind_gusts_10m"] == 14.0
    # A value the provider left out is not there at all, not 0 (WTH-004).
    assert "wind_gusts_10m" not in found.hours[1].values
    assert "visibility" not in found.hours[0].values
    assert found.content_hash and found.revision == ""


def test_hub_height_wind_is_between_the_levels_around_it_and_absent_without_them() -> None:
    values = {"wind_speed_10m": 8.0, "wind_speed_80m": 10.0, "wind_speed_120m": 12.0}
    assert provider.hub_wind(values, 100.0) == pytest.approx(11.0)
    assert provider.hub_wind(values, 80.0) == pytest.approx(10.0)
    assert provider.hub_wind({"wind_speed_10m": 8.0}, 100.0) is None
    assert provider.hub_wind(values, 150.0) is None, "180 m is missing, so nothing is invented"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_busy_or_failing_provider_counts_against_its_breaker(status: int) -> None:
    meteo, _, breaker = adapter(lambda r: httpx.Response(status))

    async def ten() -> None:
        for _ in range(10):
            with pytest.raises(provider.ProviderUnavailableError):
                await meteo.fetch(54.8, 1.9, NOW)

    asyncio.run(ten())
    assert breaker.state is BreakerState.OPEN


def test_a_refusal_or_an_unreadable_answer_is_unavailable_but_not_a_failure() -> None:
    meteo, _, breaker = adapter(lambda r: httpx.Response(400, json={"error": True}))
    with pytest.raises(provider.ProviderUnavailableError, match="refused"):
        asyncio.run(meteo.fetch(54.8, 1.9, NOW))
    garbled, _, _ = adapter(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(provider.ProviderUnavailableError, match="not JSON"):
        asyncio.run(garbled.fetch(54.8, 1.9, NOW))
    assert breaker.state is BreakerState.CLOSED


def test_no_connection_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    meteo, _, _ = adapter(refuse)
    with pytest.raises(provider.ProviderUnavailableError, match="cannot be reached"):
        asyncio.run(meteo.fetch(54.8, 1.9, NOW))


def test_an_answer_without_hours_or_coordinates_is_not_a_forecast() -> None:
    with pytest.raises(provider.ProviderUnavailableError, match="no hourly"):
        provider.parse({"latitude": 1, "longitude": 2}, NOW, b"{}")
    with pytest.raises(provider.ProviderUnavailableError, match="no coordinates"):
        provider.parse({"hourly": {"time": []}}, NOW, json.dumps({}).encode())
