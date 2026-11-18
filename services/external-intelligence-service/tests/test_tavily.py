"""The Tavily adapter, against a stand-in for the provider (EXT-001, EXT-005)."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from windops_external_intelligence import tavily
from windops_resilience import BreakerConfig, CircuitBreaker

KEY = "tvly-" + "test-not-a-real-key-000000"


def adapter(tmp_path: Path, handler: Any, key: str | None = KEY) -> tavily.Tavily:
    key_file = tmp_path / "api-key"
    if key is not None:
        key_file.write_text(key)
    return tavily.Tavily(
        key_file,
        CircuitBreaker("tavily", BreakerConfig(slow_call_seconds=10)),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


ANSWER = {
    "request_id": "req-1",
    "response_time": 1.25,
    "results": [
        {
            "url": "https://www.metoffice.gov.uk/w",
            "title": "Warnings",
            "content": "Amber warning",
            "score": 0.91,
            "published_date": "2026-11-18T06:00:00Z",
        },
        {
            "url": "https://example.com/n",
            "title": "News",
            "content": "x" * 9000,
            "score": 1.7,
            "published_date": "Tue, 17 Nov 2026 09:30:00 GMT",
        },
        {"url": "http://insecure.example/x", "title": "Dropped", "content": "", "score": 0.9},
        {"url": "https://undated.example/x", "title": "Undated", "content": "c", "score": 0.4},
    ],
}


def search(provider: tavily.Tavily, **kw: Any) -> tavily.Searched:
    fields = {
        "topic": "news",
        "time_range": "week",
        "max_results": 5,
        "include_domains": [],
        "exclude_domains": ["x.com"],
        **kw,
    }
    return asyncio.run(provider.search("severe weather offshore", **fields))


def test_a_search_sends_the_query_and_policy_and_normalizes_the_results(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANSWER)

    found = search(adapter(tmp_path, handler))
    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert body["query"] == "severe weather offshore" and body["include_published_date"] is True
    assert body["exclude_domains"] == ["x.com"] and "include_domains" not in body
    assert "country" not in body, "Tavily takes a country only for general searches"
    assert [r.url for r in found.results] == [
        "https://www.metoffice.gov.uk/w",
        "https://example.com/n",
        "https://undated.example/x",
    ]
    first, second, third = found.results
    assert first.published_at == datetime(2026, 11, 18, 6, tzinfo=UTC)
    assert second.published_at == datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    assert third.published_at is None
    assert second.score == 1.0 and len(second.content) == tavily.SNIPPET_CHARACTERS
    assert (found.request_id, found.response_seconds) == ("req-1", 1.25)
    assert len(found.content_hash) == 64


def test_a_general_search_sends_its_domains_and_country(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    search(
        adapter(tmp_path, handler),
        topic="general",
        include_domains=["iec.ch"],
        country="united kingdom",
    )
    assert seen[0]["include_domains"] == ["iec.ch"] and seen[0]["country"] == "united kingdom"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, tavily.ProviderUnavailableError),
        (503, tavily.ProviderUnavailableError),
        (401, tavily.ProviderUnavailableError),
        (432, tavily.ProviderUnavailableError),
        (400, tavily.ProviderRefusedError),
    ],
)
def test_what_the_provider_says_decides_what_follows(
    tmp_path: Path, status: int, error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "no"})

    with pytest.raises(error):
        search(adapter(tmp_path, handler))


def test_without_a_key_nothing_is_sent(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent without a key")

    with pytest.raises(tavily.ProviderUnavailableError, match="No web research provider key"):
        search(adapter(tmp_path, handler, key=None))


def test_extraction_gives_each_url_its_content_or_its_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["urls"] == [
            "https://a.example/1",
            "https://b.example/2",
            "https://c.example/3",
        ]
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://a.example/1", "raw_content": "y" * 30000}],
                "failed_results": [{"url": "https://b.example/2", "error": "timeout"}],
            },
        )

    got = asyncio.run(
        adapter(tmp_path, handler).extract(
            ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
        )
    )
    assert len(got[0].content) == tavily.EXTRACT_CHARACTERS and got[0].error == ""
    assert got[1].error == "timeout" and got[2].error == "The provider gave nothing for it"


def test_a_request_is_logged_by_its_size_never_its_text_or_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    with caplog.at_level(logging.INFO, logger="windops.service"):
        search(adapter(tmp_path, handler))
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "Research request sent" in logged and "severe weather" not in logged
    assert KEY not in logged
