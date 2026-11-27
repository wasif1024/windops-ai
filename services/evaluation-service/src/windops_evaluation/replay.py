"""Recorded external answers, given in the providers' own shapes (EVL-001, M12.6).

A regression runs in the evaluation tenant, whose External Intelligence and Weather calls come
here instead of to Tavily and Open-Meteo. So a run is repeatable, never depends on the live web,
and can plant an injected instruction where a scenario's recording says (MCP-008):

- **Baseline:** what every call gets with no recording set: a few plain search results and their
  pages, and a calm, steady forecast from the current hour, so it is always fresh.
- **A scenario's recording:** while its job's runner has set it, the answer its tool gets. A web
  search's, or a stored research record's, is the first search result; an extracted page's is
  that page, also found by the search that leads to it; a forecast's text goes in a field the
  Weather Service never reads, so it shows the text dropped, never passed on.

Documentation tools are not here: their recordings are documents in the evaluation tenant's own
library. Nothing here depends on the database.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# The tools whose answers are replayed here, by what they call.
SEARCHED = frozenset({"tavily_search_web", "tavily_get_research"})
EXTRACTED = frozenset({"tavily_extract_page"})
FORECAST = frozenset({"weather_get_forecast"})
REPLAYED = SEARCHED | EXTRACTED | FORECAST
# Search results' snippets, as Tavily gives them: a sentence or two.
SNIPPET = 400
BASELINE_PAGES = {
    "https://reference.example.org/wind/gearbox-oil-temperature": (
        "Gearbox oil temperature in wind turbines",
        "Gearbox oil temperature rises with load and ambient temperature. A rise at the same load "
        "points to the cooling circuit: the oil cooler fan, its thermostatic valve or a blocked "
        "cooler. Operators compare a turbine with its neighbours before deciding.",
    ),
    "https://reference.example.org/wind/main-bearing-condition": (
        "Main bearing condition monitoring",
        "Main bearing wear shows first as a slow temperature rise against similar turbines, then "
        "as broadband vibration. Its rate of change decides how soon it must be replaced.",
    ),
    "https://reference.example.org/wind/rotor-imbalance": (
        "Rotor imbalance and its causes",
        "Vibration at the rotor's running speed that grows with the square of speed points to "
        "mass imbalance: ice, water in a blade, or a lost balance weight.",
    ),
}


@dataclass(frozen=True)
class Recorded:
    """A scenario's recorded answer, as its definition holds it."""

    tool: str
    source: str
    text: str


def recorded(definition: Mapping[str, Any]) -> Recorded | None:
    """The answer a scenario replays here, or None: no recording, or one for a library document."""
    found = definition.get("recording")
    if not isinstance(found, Mapping) or found.get("tool") not in REPLAYED:
        return None
    return Recorded(str(found["tool"]), str(found.get("source", "")), str(found.get("text", "")))


def _sentence(text: str) -> str:
    end = text.find(". ")
    return text[: end + 1] if 0 < end < SNIPPET else text[:SNIPPET]


def search(query: str, max_results: int, active: Recorded | None) -> dict[str, Any]:
    """Tavily's search answer: the recording's page first, then the baseline's."""
    results = [
        {"url": url, "title": title, "content": _sentence(content), "score": 0.6,
         "published_date": "2026-01-15"}
        for url, (title, content) in BASELINE_PAGES.items()
    ]  # fmt: skip
    if active is not None and active.tool in SEARCHED | EXTRACTED:
        content = active.text if active.tool in SEARCHED else _sentence(active.text)
        results.insert(0, {"url": active.source, "title": _sentence(active.text)[:200],
                           "content": content[:SNIPPET * 2], "score": 0.9,
                           "published_date": "2026-05-04"})  # fmt: skip
    return {
        "query": query,
        "results": results[: max(1, min(max_results, 10))],
        "response_time": 0.01,
        "request_id": "recorded",
    }


def extract(urls: Sequence[str], active: Recorded | None) -> dict[str, Any]:
    """Tavily's extract answer: the recording's page, or a baseline page, or a failure."""
    pages = {url: content for url, (_, content) in BASELINE_PAGES.items()}
    if active is not None and active.tool in SEARCHED | EXTRACTED:
        pages[active.source] = active.text
    return {
        "results": [{"url": u, "raw_content": pages[u], "published_date": "2026-05-04"}
                    for u in urls if u in pages],
        "failed_results": [{"url": u, "error": "Not recorded"} for u in urls if u not in pages],
    }  # fmt: skip


def forecast(
    latitude: float, longitude: float, past_days: int, forecast_days: int, now: datetime,
    active: Recorded | None,
) -> dict[str, Any]:  # fmt: skip
    """Open-Meteo's hourly answer: a calm, steady wind from now, so it is always fresh. A
    recording's text goes where the Weather Service never reads it (MCP-008)."""
    start = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    start -= timedelta(days=max(0, past_days))
    hours = 24 * (max(0, past_days) + max(1, forecast_days))
    times, wind = [], []
    for index in range(hours):
        times.append((start + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M"))
        wind.append(round(7.0 + 2.0 * math.sin(index / 6.0), 1))
    body: dict[str, Any] = {
        "latitude": latitude, "longitude": longitude, "elevation": 0.0,
        "hourly_units": {"wind_speed_10m": "m/s"},
        "hourly": {
            "time": times,
            "wind_speed_10m": wind,
            "wind_speed_80m": [round(w * 1.25, 1) for w in wind],
            "wind_speed_120m": [round(w * 1.32, 1) for w in wind],
            "wind_speed_180m": [round(w * 1.4, 1) for w in wind],
            "wind_gusts_10m": [round(w * 1.4, 1) for w in wind],
            "wind_direction_10m": [240.0] * hours,
            "temperature_2m": [9.0] * hours,
            "precipitation": [0.0] * hours,
            "visibility": [20000.0] * hours,
            "cape": [0.0] * hours,
        },
    }  # fmt: skip
    if active is not None and active.tool in FORECAST:
        body["notes"] = active.text
    return body
