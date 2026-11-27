"""The live suite's checks, against stand-ins for the platform's services (EVL-001, M12.6b)."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from windops.external_intelligence.v1 import external_intelligence_pb2 as ei
from windops.weather.v1 import weather_pb2 as wx
from windops_evaluation import live
from windops_evaluation.live_runner import LiveSettings, manual_run_id

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


def forecast(**changes: Any) -> wx.GetForecastResponse:
    answer = wx.GetForecastResponse(
        state="fresh", age_seconds=900,
        forecast=wx.Forecast(forecast_id="fct_" + "a" * 20, provider="open-meteo"),
        points=[wx.ForecastPoint(wind_speed_10m_mps=8.0, wind_speed_hub_mps=11.0,
                                 temperature_2m_c=7.0) for _ in range(24)],
        summary=wx.ForecastSummary(hours_covered=24, hours_wanted=24),
    )  # fmt: skip
    for name, value in changes.items():
        if name == "points":
            del answer.points[:]
            answer.points.extend(value)
        elif hasattr(value, "CopyFrom"):
            getattr(answer, name).CopyFrom(value)
        else:
            setattr(answer, name, value)
    return answer


def research(state: str = "complete", urls: tuple[str, ...] = ("https://a.example/x",)) -> Any:
    return ei.ResearchResponse(research=ei.ResearchRecord(
        research_id="res_1", state=state,
        sources=[ei.Source(source_id=f"src_{i}", url=u) for i, u in enumerate(urls)],
    ))  # fmt: skip


class Providers:
    def __init__(self, **broken: Exception) -> None:
        self.broken = broken
        self.asked: list[str] = []

    async def _call(self, name: str, answer: Any) -> Any:
        self.asked.append(name)
        if name in self.broken:
            raise self.broken[name]
        return answer

    async def a_turbine(self) -> str:
        return str(await self._call("turbine", "dogger-east-t01"))

    async def forecast(self, turbine_id: str, start: datetime, end: datetime) -> Any:
        return await self._call("forecast", forecast())

    async def research(self, query: str, topic: str, key: str) -> Any:
        assert "north-sea" not in query and topic in ("technical_advisory",)
        return await self._call("research", research())

    async def extract(self, research_id: str, source_id: str, key: str) -> Any:
        return await self._call("extract", ei.ExtractResponse(state="complete", extractions=[
            ei.Extraction(source_id=source_id, state="extracted", content="A page")]))  # fmt: skip


def test_a_run_with_every_live_service_answering_well_passes() -> None:
    providers = Providers()
    checks = asyncio.run(live.run(providers, NOW, "live-1"))
    assert live.outcome(checks) == "passed", checks
    assert providers.asked == ["turbine", "forecast", "research", "extract"]
    assert {c.name for c in checks} == {
        "forecast_fresh", "forecast_covers_window", "forecast_plausible", "research_answered",
        "research_sources_secure", "page_extracted",
    }  # fmt: skip


def test_a_service_that_cannot_be_reached_is_a_failed_check_never_a_skipped_one() -> None:
    """SRS 7.7."""
    checks = asyncio.run(live.run(Providers(forecast=TimeoutError("slow")), NOW, "live-2"))
    failed = {c.name: c.detail for c in checks if not c.passed}
    assert "forecast_fresh" in failed and "TimeoutError" in failed["forecast_fresh"]
    assert live.outcome(checks) == "failed"
    checks = asyncio.run(live.run(Providers(research=ConnectionError("down")), NOW, "live-3"))
    assert [c.name for c in checks if not c.passed] == ["research_answered"]


@pytest.mark.parametrize(
    ("answer", "failed"),
    [
        (forecast(state="stale"), "forecast_fresh"),
        (forecast(forecast=wx.Forecast(forecast_id="fct_x", provider="recorded")),
         "forecast_fresh"),
        (forecast(summary=wx.ForecastSummary(hours_covered=10, hours_wanted=24)),
         "forecast_covers_window"),
        (forecast(points=[wx.ForecastPoint(wind_speed_10m_mps=300.0, wind_speed_hub_mps=9.0)]),
         "forecast_plausible"),
        (forecast(points=[wx.ForecastPoint(wind_speed_10m_mps=8.0)]), "forecast_plausible"),
    ],
)  # fmt: skip
def test_a_forecast_must_be_fresh_from_the_provider_and_physical(answer: Any, failed: str) -> None:
    assert [c.name for c in live.judge_forecast(answer) if not c.passed] == [failed]


def test_research_must_be_answered_from_secure_sources() -> None:
    assert all(c.passed for c in live.judge_research(research().research))
    unavailable = live.judge_research(research(state="unavailable", urls=()).research)
    assert not any(c.passed for c in unavailable)
    insecure = live.judge_research(research(urls=("http://a.example/x",)).research)
    assert [c.name for c in insecure if not c.passed] == ["research_sources_secure"]


def test_the_live_suite_never_runs_where_the_providers_are_recorded() -> None:
    assert LiveSettings.from_env({}) is None
    assert LiveSettings.from_env({"WINDOPS_LIVE_SUITE_TENANT": "north-sea",
                                  "WINDOPS_LIVE_SUITE_HOURS": "24"}) == LiveSettings(
        "north-sea", 24.0)  # fmt: skip
    with pytest.raises(ValueError, match="recorded"):
        LiveSettings.from_env({
            "WINDOPS_LIVE_SUITE_TENANT": "windops-evaluation",
            "WINDOPS_REPLAY_TENANTS": "windops-evaluation",
            "WINDOPS_REPLAY_URL": "https://e:8443/replay",
        })  # fmt: skip


def test_a_retried_request_finds_the_first_run() -> None:
    """GRP-007: the same key, the same run."""
    assert manual_run_id("k-1") == manual_run_id("k-1") != manual_run_id("k-2")
    assert manual_run_id("") != manual_run_id("")


def test_with_no_schedule_the_task_waits_and_never_stops_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background task that returns stops the service, so an off schedule only waits."""
    from windops_evaluation import live_runner

    async def none_kept(engine: Any, limit: int) -> list[Any]:
        return []

    monkeypatch.setattr(live_runner, "latest", none_kept)

    async def scenario() -> bool:
        stop = asyncio.Event()
        task = asyncio.create_task(
            live_runner.schedule(cast(Any, None), Providers(), LiveSettings("north-sea", 0.0), stop)
        )
        await asyncio.sleep(0.05)
        waiting = not task.done()
        stop.set()
        await task
        return waiting

    assert asyncio.run(scenario())
