"""The Tavily adapter (EXT-001): search and extraction, normalized.

Only the generic query and the domain policy go out (EXT-006). Each request is logged by its
kind and size, never its text (EGR-004). TLS is always verified. The key is read from its
mounted file at each request, so a replaced key needs no restart, and only this service's pod
mounts it (EXT-005). A 429, a 5xx, a timeout or no connection counts against the provider's
breaker; a refused key, or Tavily's plan limits, make research unavailable; any other refusal is
the request's own.
"""

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from windops_resilience import BreakerOpenError, CircuitBreaker

logger = logging.getLogger("windops.service")
PROVIDER = "tavily"
SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"
SEARCH_SECONDS, EXTRACT_SECONDS = 15.0, 30.0
# What is kept of a page: enough to judge it, too little to copy it.
SNIPPET_CHARACTERS, EXTRACT_CHARACTERS = 4000, 20000


class ProviderUnavailableError(Exception):
    """The provider cannot be reached, has no key, or its plan's limit is reached."""


class ProviderRefusedError(Exception):
    """The provider refused the request itself."""


@dataclass(frozen=True)
class Found:
    url: str
    title: str
    content: str
    score: float
    published_at: datetime | None


@dataclass(frozen=True)
class Searched:
    results: tuple[Found, ...]
    request_id: str
    response_seconds: float
    content_hash: str


@dataclass(frozen=True)
class Extracted:
    url: str
    content: str
    published_at: datetime | None
    error: str = ""


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return 0.0
    return float(value)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for parse in (
        datetime.fromisoformat,
        lambda t: datetime.strptime(t, "%a, %d %b %Y %H:%M:%S %Z"),
    ):
        try:
            parsed = parse(text)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


class Tavily:
    def __init__(
        self,
        key_file: Path | None,
        breaker: CircuitBreaker,
        http: httpx.AsyncClient,
        search_url: str = SEARCH_URL,
        extract_url: str = EXTRACT_URL,
    ) -> None:
        """With no key file, no key is sent: the Evaluation Service's recordings need none, and
        Tavily's key must never go anywhere but Tavily (M12.6)."""
        self._key_file, self._breaker, self._http = key_file, breaker, http
        self._search_url, self._extract_url = search_url, extract_url

    def _key(self) -> str:
        if self._key_file is None:
            return ""
        try:
            key = self._key_file.read_text().strip()
        except OSError:
            key = ""
        if not key:
            raise ProviderUnavailableError("No web research provider key is configured")
        return key

    async def _post(
        self, url: str, body: dict[str, Any], timeout: float, kind: str, size: int
    ) -> Any:
        key = self._key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}

        async def send() -> httpx.Response:
            try:
                response = await self._http.post(url, json=body, timeout=timeout, headers=headers)
            except httpx.TimeoutException as error:
                raise TimeoutError("The provider did not answer in time") from error
            except httpx.TransportError as error:
                raise ConnectionError("The provider cannot be reached") from error
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            return response

        logger.info(
            "Research request sent", extra={"provider": PROVIDER, "kind": kind, "size": size}
        )
        try:
            response = await self._breaker.call(send)
        except (BreakerOpenError, TimeoutError, ConnectionError, httpx.HTTPError) as error:
            raise ProviderUnavailableError("The provider cannot be reached right now") from error
        if response.status_code in (401, 403):
            raise ProviderUnavailableError("The provider refused the key")
        if response.status_code in (432, 433, 434):
            raise ProviderUnavailableError("The provider's plan limit is reached")
        if response.status_code != 200:
            raise ProviderRefusedError(f"The provider refused the request: {response.status_code}")
        try:
            return response.json(), response.content
        except ValueError as error:
            raise ProviderUnavailableError("The provider's answer is not JSON") from error

    async def search(
        self,
        query: str,
        *,
        topic: str,
        time_range: str,
        max_results: int,
        include_domains: list[str],
        exclude_domains: list[str],
        country: str = "",
    ) -> Searched:
        body: dict[str, Any] = {
            "query": query,
            "topic": topic,
            "time_range": time_range,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
            "exclude_domains": exclude_domains,
        }
        if include_domains:
            body["include_domains"] = include_domains
        if country and topic == "general":
            body["country"] = country
        answer, raw = await self._post(self._search_url, body, SEARCH_SECONDS, "search", len(query))
        rows = answer.get("results") if isinstance(answer, dict) else None
        if not isinstance(rows, list):
            raise ProviderUnavailableError("The provider's answer has no results")
        found = tuple(
            Found(
                url=row["url"],
                title=_text(row.get("title"), 500),
                content=_text(row.get("content"), SNIPPET_CHARACTERS),
                score=min(max(_number(row.get("score")), 0.0), 1.0),
                published_at=_date(row.get("published_date")),
            )
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("url"), str)
            and row["url"].startswith("https://")
        )
        return Searched(
            found,
            str(answer.get("request_id", "")),
            _number(answer.get("response_time")),
            hashlib.sha256(raw).hexdigest(),
        )

    async def extract(self, urls: list[str]) -> list[Extracted]:
        answer, _ = await self._post(
            self._extract_url,
            {
                "urls": urls,
                "extract_depth": "basic",
                "format": "markdown",
                "include_published_date": True,
            },
            EXTRACT_SECONDS,
            "extract",
            len(urls),
        )
        found = {
            row["url"]: Extracted(
                row["url"],
                _text(row.get("raw_content"), EXTRACT_CHARACTERS),
                _date(row.get("published_date")),
            )
            for row in answer.get("results", [])
            if isinstance(row, dict) and "url" in row
        }
        for row in answer.get("failed_results", []):
            if isinstance(row, dict) and "url" in row:
                found[row["url"]] = Extracted(
                    row["url"], "", None, _text(row.get("error"), 300) or "failed"
                )
        return [
            found.get(url, Extracted(url, "", None, "The provider gave nothing for it"))
            for url in urls
        ]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
