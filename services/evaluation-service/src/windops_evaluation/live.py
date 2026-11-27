"""The live suite: the real Tavily and weather providers, through the platform's own services
(EVL-001, M12.6b).

The regression never touches them: it runs on recordings, so it is repeatable. So on a schedule,
the Evaluation Service asks, as itself, in a tenant whose providers are live:

- the Weather Service for a turbine's forecast: fresh, covering the window asked for, from the
  provider, with values a real atmosphere can have;
- the External Intelligence Service to research a fixed, generic question: answered, from
  secure sources, which it marks as untrusted where they are; and to read the first source.

Each answer was parsed by the service from the provider's own format, so a provider that changed
its format fails here, and its recordings need renewing. It spends a search and an extraction a
run, from the tenant's daily research allowance. A run's outcome is passed only when every check
passed; a service that cannot be reached is a failed check, never a skipped one (SRS 7.7).
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

# The fixed, generic question researched each run: nothing in it names a tenant or a site.
QUESTION = "wind turbine gearbox oil cooling fault guidance"
TOPIC = "technical_advisory"
FORECAST_HOURS = 24
# What a real atmosphere can have, at a turbine's site.
LIMITS = {
    "wind_speed_10m_mps": (0.0, 75.0), "wind_speed_hub_mps": (0.0, 90.0),
    "wind_gusts_10m_mps": (0.0, 110.0), "wind_direction_10m_deg": (0.0, 360.0),
    "temperature_2m_c": (-70.0, 60.0), "precipitation_mm": (0.0, 300.0),
    "visibility_m": (0.0, 200_000.0), "cape_j_per_kg": (0.0, 10_000.0),
}  # fmt: skip


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


def outcome(checks: Sequence[Check]) -> str:
    """Passed only when there are checks, and every one passed."""
    return "passed" if checks and all(c.passed for c in checks) else "failed"


def judge_forecast(answer: Any) -> list[Check]:
    """A forecast from the provider: fresh, covering the window, every value physical."""
    found = answer.forecast
    live = answer.state == "fresh" and bool(found.forecast_id) and found.provider == "open-meteo"
    checks = [Check("forecast_fresh", live,
                    f"{answer.state} from {found.provider or 'nobody'}, {answer.age_seconds} s "
                    f"old. {answer.note}".strip())]  # fmt: skip
    summary = answer.summary
    covered = summary.hours_wanted > 0 and summary.hours_covered >= summary.hours_wanted
    checks.append(Check("forecast_covers_window", covered,
                        f"{summary.hours_covered} of {summary.hours_wanted} hours"))  # fmt: skip
    outside, hub = [], 0
    for point in answer.points:
        for name, (low, high) in LIMITS.items():
            if point.HasField(name) and not low <= getattr(point, name) <= high:
                outside.append(f"{name}={getattr(point, name)}")
        hub += point.HasField("wind_speed_hub_mps")
    plausible = bool(answer.points) and not outside and hub > 0
    checks.append(Check("forecast_plausible", plausible,
                        f"{len(answer.points)} hours, {hub} with hub-height wind; outside: "
                        f"{outside[:5] or 'none'}"))  # fmt: skip
    return checks


def judge_research(record: Any) -> list[Check]:
    """A research answer from the provider: complete, from secure sources."""
    sources = list(record.sources)
    insecure = [s.url for s in sources if not s.url.startswith("https://")]
    return [
        Check("research_answered", record.state == "complete" and bool(sources),
              f"{record.state}, {len(sources)} sources. {record.data_gap}".strip()),
        Check("research_sources_secure", bool(sources) and not insecure,
              f"insecure: {insecure[:3] or 'none'}"),
    ]  # fmt: skip


def judge_extraction(answer: Any) -> list[Check]:
    """The first source's page, read from the provider."""
    read = [e for e in answer.extractions if e.state == "extracted" and e.content]
    return [Check("page_extracted", answer.state == "complete" and bool(read),
                  f"{answer.state}, {len(read)} of {len(answer.extractions)} read. "
                  f"{answer.data_gap}".strip())]  # fmt: skip


class Providers(Protocol):
    """The platform's services, asked as the Evaluation Service itself."""

    async def a_turbine(self) -> str: ...
    async def forecast(self, turbine_id: str, start: datetime, end: datetime) -> Any: ...
    async def research(self, query: str, topic: str, key: str) -> Any: ...
    async def extract(self, research_id: str, source_id: str, key: str) -> Any: ...


async def _asked(name: str, call: Callable[[], Awaitable[Any]]) -> tuple[Any, Check | None]:
    try:
        return await call(), None
    except Exception as error:  # Any failure is a failed check, never a crash.
        return None, Check(name, False, f"{type(error).__name__}: {str(error)[:300]}")


async def run(providers: Providers, now: datetime, run_id: str) -> list[Check]:
    """Every check of one run. The keys make a retried run's research the same research."""
    checks: list[Check] = []
    turbine, failed = await _asked("turbine_found", providers.a_turbine)
    if failed or not turbine:
        return [*checks, failed or Check("turbine_found", False, "The tenant has no turbine")]
    window = (now, now + timedelta(hours=FORECAST_HOURS))
    forecast, failed = await _asked("forecast_fresh",
                                    lambda: providers.forecast(turbine, *window))  # fmt: skip
    checks += [failed] if failed else judge_forecast(forecast)
    research, failed = await _asked(
        "research_answered", lambda: providers.research(QUESTION, TOPIC, f"{run_id}-research")
    )
    if failed:
        return [*checks, failed]
    record = research.research
    checks += judge_research(record)
    if record.state == "complete" and record.sources:
        page, failed = await _asked("page_extracted", lambda: providers.extract(
            record.research_id, record.sources[0].source_id, f"{run_id}-extract"))  # fmt: skip
        checks += [failed] if failed else judge_extraction(page)
    else:
        checks.append(Check("page_extracted", False, "There was no source to read"))
    return checks
