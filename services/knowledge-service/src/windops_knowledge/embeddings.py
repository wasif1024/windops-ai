"""The embedding adapter (LLM-011): one interface, and OpenAI behind it (ADR 0011).

A provider turns passages into vectors, and says which model and version made them, which every
Qdrant point records. Changing provider is a new adapter and a re-index, nothing else.

Only approved passages go out: the service sends nothing above the tenant's allowed
classification, and nothing an upload's credential scan would refuse (SRS 9.6). Each request is
logged with its size, never its text (EGR-004). TLS is always verified. A 429, a 5xx, a timeout or
no connection counts against the provider's breaker; any other refusal is the request's fault, and
retrying it cannot help.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from windops_resilience import BreakerOpenError, CircuitBreaker

logger = logging.getLogger("windops.service")
OPENAI_URL = "https://api.openai.com/v1/embeddings"
# The SRS's starting model (LLM-011), and its full size.
OPENAI_MODEL = "text-embedding-3-large"
OPENAI_DIMENSIONS = 3072
# This adapter's own version: how it calls the model. Recorded with each point.
ADAPTER_VERSION = "openai-embeddings-1"
# Inputs in one request: well inside the provider's limits on count and tokens.
BATCH = 64
TIMEOUT_SECONDS = 30.0


class EmbeddingUnavailableError(Exception):
    """The provider cannot be reached now, or has no key: try again later."""


class EmbeddingRefusedError(Exception):
    """The provider refused the request itself: trying again will not help."""


@dataclass(frozen=True)
class Embedded:
    vectors: list[list[float]]
    # What the provider says made them.
    model: str
    tokens: int


class Embedder(Protocol):
    provider: str
    model: str
    version: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> Embedded: ...


class OpenAIEmbedder:
    provider = "openai"
    model = OPENAI_MODEL
    version = ADAPTER_VERSION
    dimensions = OPENAI_DIMENSIONS

    def __init__(self, url: str, key_file: Path, breaker: CircuitBreaker,
                 http: httpx.AsyncClient) -> None:  # fmt: skip
        self._url, self._key_file, self._breaker, self._http = url, key_file, breaker, http

    def _key(self) -> str:
        """Read each time, so a replaced key is used without a restart."""
        try:
            key = self._key_file.read_text().strip()
        except OSError:
            key = ""
        if not key:
            raise EmbeddingUnavailableError("No embedding provider key is configured")
        return key

    async def embed(self, texts: Sequence[str]) -> Embedded:
        vectors: list[list[float]] = []
        model, tokens = self.model, 0
        for start in range(0, len(texts), BATCH):
            batch = list(texts[start : start + BATCH])
            embedded = await self._request(batch)
            vectors.extend(embedded.vectors)
            model, tokens = embedded.model, tokens + embedded.tokens
        return Embedded(vectors, model, tokens)

    async def _request(self, batch: list[str]) -> Embedded:
        key = self._key()
        body = {"model": self.model, "input": batch, "encoding_format": "float"}

        async def send() -> httpx.Response:
            try:
                response = await self._http.post(
                    self._url, json=body, timeout=TIMEOUT_SECONDS,
                    headers={"Authorization": f"Bearer {key}"},
                )  # fmt: skip
            except httpx.TimeoutException as error:
                raise TimeoutError("The embedding provider did not answer in time") from error
            except httpx.TransportError as error:
                raise ConnectionError("The embedding provider cannot be reached") from error
            if response.status_code == 429 or response.status_code >= 500:
                # Counted by the breaker as the provider failing, not as a refusal.
                response.raise_for_status()
            return response

        logger.info("Embedding request sent", extra={
            "provider": self.provider, "model": self.model, "inputs": len(batch),
            "characters": sum(len(t) for t in batch),
        })  # fmt: skip
        try:
            response = await self._breaker.call(send)
        except (BreakerOpenError, TimeoutError, ConnectionError, httpx.HTTPError) as error:
            raise EmbeddingUnavailableError(
                "The embedding provider cannot be reached right now"
            ) from error
        if response.status_code in (401, 403):
            raise EmbeddingUnavailableError("The embedding provider refused the key")
        if response.status_code != 200:
            raise EmbeddingRefusedError(
                f"The embedding provider refused the request: {response.status_code}"
            )
        return self._parse(response, len(batch))

    def _parse(self, response: httpx.Response, count: int) -> Embedded:
        try:
            answer = response.json()
            rows = sorted(answer["data"], key=lambda row: row["index"])
            vectors = [[float(v) for v in row["embedding"]] for row in rows]
            model = str(answer.get("model") or self.model)
            tokens = int(answer.get("usage", {}).get("total_tokens", 0))
        except (ValueError, KeyError, TypeError) as error:
            raise EmbeddingUnavailableError(
                "The embedding provider's answer is unreadable"
            ) from error
        if len(vectors) != count or any(len(v) != self.dimensions for v in vectors):
            raise EmbeddingUnavailableError("The embedding provider's answer is incomplete")
        if not all(math.isfinite(x) for v in vectors for x in v):
            raise EmbeddingUnavailableError("The embedding provider's answer is not numbers")
        return Embedded(vectors, model, tokens)
