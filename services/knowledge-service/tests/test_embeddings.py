"""The OpenAI embedding adapter, against a stand-in for the provider (LLM-011, ADR 0011)."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from windops_knowledge import embeddings
from windops_resilience import BreakerConfig, CircuitBreaker

KEY = "sk-test-not-a-real-key-0000000000"
URL = "https://embeddings.test/v1/embeddings"


def answer(request: httpx.Request, dimensions: int = embeddings.OPENAI_DIMENSIONS) -> Any:
    inputs = json.loads(request.content)["input"]
    # Out of order on purpose: the adapter must put them back by index.
    rows = [{"index": i, "embedding": [float(i)] * dimensions} for i in range(len(inputs))]
    return {"data": list(reversed(rows)), "model": "text-embedding-3-large",
            "usage": {"total_tokens": 7 * len(inputs)}}  # fmt: skip


def adapter(tmp_path: Path, handler: Any, key: str | None = KEY,
            breaker: CircuitBreaker | None = None) -> embeddings.OpenAIEmbedder:  # fmt: skip
    key_file = tmp_path / "api-key"
    if key is not None:
        key_file.write_text(key + "\n")
    return embeddings.OpenAIEmbedder(
        URL, key_file, breaker or CircuitBreaker("openai", BreakerConfig(slow_call_seconds=10)),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )  # fmt: skip


def test_passages_are_sent_with_the_key_and_come_back_in_order(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer(request))

    texts = [f"passage {n}" for n in range(embeddings.BATCH + 3)]
    embedded = asyncio.run(adapter(tmp_path, handler).embed(texts))
    assert len(seen) == 2, "in batches"
    first = json.loads(seen[0].content)
    assert first == {"model": "text-embedding-3-large", "input": texts[: embeddings.BATCH],
                     "encoding_format": "float"}  # fmt: skip
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert [v[0] for v in embedded.vectors] == [
        float(n % embeddings.BATCH) for n in range(len(texts))
    ]
    assert embedded.model == "text-embedding-3-large" and embedded.tokens == 7 * len(texts)


def test_without_a_key_nothing_is_sent(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent without a key")

    for key in (None, "  "):
        with pytest.raises(embeddings.EmbeddingUnavailableError, match="No embedding provider key"):
            asyncio.run(adapter(tmp_path, handler, key).embed(["x"]))


def test_a_replaced_key_is_used_without_a_restart(tmp_path: Path) -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["authorization"])
        return httpx.Response(200, json=answer(request))

    embedder = adapter(tmp_path, handler)
    asyncio.run(embedder.embed(["x"]))
    (tmp_path / "api-key").write_text("sk-test-replaced-key-000000000000")
    asyncio.run(embedder.embed(["x"]))
    assert keys == [f"Bearer {KEY}", "Bearer sk-test-replaced-key-000000000000"]


@pytest.mark.parametrize(("status", "error"), [
    (429, embeddings.EmbeddingUnavailableError),
    (503, embeddings.EmbeddingUnavailableError),
    (401, embeddings.EmbeddingUnavailableError),
    (400, embeddings.EmbeddingRefusedError),
])  # fmt: skip
def test_what_the_provider_says_decides_whether_to_try_again(
    tmp_path: Path, status: int, error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "no"}})

    with pytest.raises(error):
        asyncio.run(adapter(tmp_path, handler).embed(["x"]))


def test_no_connection_counts_against_the_breaker_until_it_opens(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("no route", request=request)

    breaker = CircuitBreaker("openai", BreakerConfig(slow_call_seconds=10, minimum_calls=3,
                                                     window_size=3))  # fmt: skip
    embedder = adapter(tmp_path, handler, breaker=breaker)
    for _ in range(5):
        with pytest.raises(embeddings.EmbeddingUnavailableError):
            asyncio.run(embedder.embed(["x"]))
    assert calls == 3, "once open, the breaker sends nothing"


@pytest.mark.parametrize("broken", [
    {"data": []},
    {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
    {"nothing": True},
])  # fmt: skip
def test_an_answer_that_is_not_one_vector_each_of_the_models_size_is_not_used(
    tmp_path: Path, broken: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=broken)

    with pytest.raises(embeddings.EmbeddingUnavailableError):
        asyncio.run(adapter(tmp_path, handler).embed(["x"]))


def test_a_request_is_logged_by_its_size_never_its_text_or_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answer(request))

    with caplog.at_level(logging.INFO, logger="windops.service"):
        asyncio.run(adapter(tmp_path, handler).embed(["the confidential passage"]))
    sent = [r for r in caplog.records if r.getMessage() == "Embedding request sent"]
    assert len(sent) == 1 and sent[0].__dict__["inputs"] == 1
    assert sent[0].__dict__["characters"] == len("the confidential passage")
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "confidential passage" not in logged and KEY not in logged
