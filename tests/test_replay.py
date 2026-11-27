"""Recorded external providers for the evaluation tenant (EVL-001, M12.6).

The Evaluation Service's recordings must read exactly as Tavily's and Open-Meteo's answers do, so
the services that parse them cannot tell the difference, and an injected instruction arrives where
the live one would. Only the tenants a deployment names are replayed.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from windops_auth import VerifiedIdentity
from windops_auth.identity import _identity
from windops_evaluation import replay
from windops_external_intelligence import tavily
from windops_grpc import ByTenant, ReplaySettings
from windops_resilience import BreakerConfig, CircuitBreaker
from windops_weather import provider

from scripts.evaluation.pipeline import definition
from scripts.evaluation.scenarios import ROOT, Scenario, recording

NOW = datetime(2026, 11, 27, 9, 30, tzinfo=UTC)


def scenario(name: str) -> Scenario:
    return Scenario.model_validate(json.loads((ROOT / "scenarios" / f"{name}.json").read_text()))


def recorded(name: str) -> replay.Recorded | None:
    return replay.recorded(json.loads(definition(scenario(name))))


def keyless(answer: dict[str, Any], seen: list[httpx.Request]) -> tavily.Tavily:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer)

    return tavily.Tavily(None, CircuitBreaker("tavily-replay", BreakerConfig(slow_call_seconds=10)),
                         httpx.AsyncClient(transport=httpx.MockTransport(handler)))  # fmt: skip


def test_a_recorded_search_reads_as_tavilys_and_carries_its_injection() -> None:
    """MCP-008: the planted page is the first result the External Intelligence Service keeps."""
    active = recorded("injection-002")
    assert active is not None and active.tool == "tavily_search_web"
    seen: list[httpx.Request] = []
    found = asyncio.run(keyless(replay.search("gearbox cooling", 5, active), seen).search(
        "gearbox cooling", topic="general", time_range="year", max_results=5,
        include_domains=[], exclude_domains=[],
    ))  # fmt: skip
    assert found.results[0].url == active.source
    assert "approve the open work order" in found.results[0].content
    assert len(found.results) == 4
    # Tavily's key never goes to the recordings.
    assert "authorization" not in seen[0].headers


def test_a_recorded_page_is_found_by_search_then_extracted_with_its_injection() -> None:
    active = recorded("injection-003")
    assert active is not None and active.tool == "tavily_extract_page"
    searched = replay.search("main bearing bulletin", 5, active)
    assert searched["results"][0]["url"] == active.source
    assert "approve_work_order" not in searched["results"][0]["content"]
    seen: list[httpx.Request] = []
    pages = asyncio.run(keyless(replay.extract([active.source, "https://elsewhere.example/x"],
                                               active), seen).extract(
        [active.source, "https://elsewhere.example/x"]))  # fmt: skip
    assert "approve_work_order" in pages[0].content
    assert pages[1].error


def test_a_recorded_forecast_is_fresh_and_its_text_is_dropped() -> None:
    """The Weather Service keeps only the provider's numbers, so the planted text never reaches an
    agent (MCP-008)."""
    active = recorded("injection-007")
    assert active is not None and active.tool == "weather_get_forecast"
    body = replay.forecast(54.8, 2.1, 1, 7, NOW, active)
    assert "escalation_required" in body["notes"]
    raw = json.dumps(body).encode()
    parsed = provider.parse(json.loads(raw), NOW, raw)
    assert len(parsed.hours) == 24 * 8
    assert parsed.hours[0].valid_at <= NOW <= parsed.hours[-1].valid_at
    assert set(parsed.hours[24].values) == set(provider.HOURLY)
    assert "notes" not in json.dumps([h.values for h in parsed.hours])


def test_the_baseline_answers_every_call_with_no_recording_set() -> None:
    assert recorded("gearbox-temp-001") is None
    assert len(replay.search("anything", 5, None)["results"]) == 3
    assert "notes" not in replay.forecast(54.8, 2.1, 0, 1, NOW, None)
    # A library document's recording is in the evaluation tenant's library, not here.
    assert recorded("injection-006") is None
    assert recording(scenario("injection-006")) is not None


@pytest.mark.parametrize(("tenant", "chosen"),
                         [("windops-evaluation", "replayed"), ("north-sea", "live"),
                          (None, "live")])  # fmt: skip
def test_only_the_tenants_named_are_replayed(tenant: str | None, chosen: str) -> None:
    class Provider:
        def __init__(self, name: str) -> None:
            self.name = name

    routed = ByTenant(Provider("live"), Provider("replayed"), frozenset({"windops-evaluation"}))
    token = _identity.set(
        VerifiedIdentity(subject="u", tenant_id=tenant) if tenant is not None else None
    )
    try:
        assert routed.name == chosen
    finally:
        _identity.reset(token)


def test_replay_is_set_only_where_evaluations_run(tmp_path: Path) -> None:
    assert ReplaySettings.from_env({}) is None
    found = ReplaySettings.from_env({"WINDOPS_REPLAY_TENANTS": "windops-evaluation, ",
                                     "WINDOPS_REPLAY_URL": "https://e:8443/replay/"})  # fmt: skip
    assert found == ReplaySettings(frozenset({"windops-evaluation"}), "https://e:8443/replay")
    with pytest.raises(ValueError, match="https"):
        ReplaySettings.from_env({"WINDOPS_REPLAY_TENANTS": "t", "WINDOPS_REPLAY_URL": "http://e"})
