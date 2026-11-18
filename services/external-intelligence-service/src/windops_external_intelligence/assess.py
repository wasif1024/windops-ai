"""What each source is worth, flagged rather than hidden (EXT-004).

- **stale:** published longer ago than its topic allows: a forecast within 6 hours, a
  severe-weather notice within a day, an OEM bulletin within three years, and so on.
- **undated:** the provider could not tell when it was published, so its age is unknown.
- **low_confidence:** the provider ranks it as a weak match, and it is not from an authoritative
  domain, such as a meteorological office, a standards body or a regulator.
- **conflicting:** sources from the same search give different weather warning levels.
- **instructions_to_ai:** its text addresses an AI, as an injected instruction does. It is still
  only text, and the MCP layer wraps it as untrusted (windops-mcp).

Nothing is dropped for a flag: the reader sees each, with its note, and decides.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from windops_screening import screen

TOPICS = {
    # Tavily's topic, how far back to search, and how old a source may be before it is stale.
    "forecast": ("news", "day", timedelta(hours=6)),
    "severe_weather": ("news", "week", timedelta(days=1)),
    "oem_bulletin": ("general", "year", timedelta(days=3 * 365)),
    "technical_advisory": ("general", "year", timedelta(days=2 * 365)),
    "industry": ("news", "month", timedelta(days=90)),
    "regulatory": ("general", "year", timedelta(days=365)),
    "environmental": ("news", "month", timedelta(days=180)),
}
# Below this, the provider's own relevance is weak.
LOW_SCORE = 0.5
# Authoritative for what this service researches. A source here is trusted, and never low
# confidence on its score alone. Subdomains count.
TRUSTED = frozenset(
    {
        "metoffice.gov.uk",
        "gov.uk",
        "ecmwf.int",
        "wmo.int",
        "meteoalarm.org",
        "eumetsat.int",
        "noaa.gov",
        "weather.gov",
        "aemet.es",
        "dwd.de",
        "meteofrance.com",
        "knmi.nl",
        "dmi.dk",
        "met.no",
        "ncm.gov.sa",
        "iec.ch",
        "iso.org",
        "europa.eu",
        "hse.gov.uk",
        "osha.gov",
        "irena.org",
        "iea.org",
        "windeurope.org",
        "gwec.net",
        "nrel.gov",
        "energy.gov",
    }
)
# Never searched, whatever a tenant allows: user-generated content is no source for operations.
BLOCKED = frozenset(
    {
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "x.com",
        "twitter.com",
        "reddit.com",
        "pinterest.com",
        "quora.com",
        "youtube.com",
    }
)
# Weather warning levels, lowest first, as most European services name them.
_LEVELS = ("yellow", "amber", "orange", "red")
_WARNING = re.compile(
    r"\b(yellow|amber|orange|red)\s+(?:weather\s+)?(?:warning|alert|level)\b", re.IGNORECASE
)


@dataclass
class Assessed:
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def flag(self, name: str, note: str) -> None:
        if name not in self.flags:
            self.flags.append(name)
            self.notes.append(note)


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def under(domain: str, listed: frozenset[str] | set[str]) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in listed)


def warning_level(text: str) -> str:
    """The highest warning level a text names, or "" for none."""
    found = {m.group(1).lower() for m in _WARNING.finditer(text)}
    # Orange and amber are the same level, by two names.
    ranked = [level for level in _LEVELS if level in found]
    if not ranked:
        return ""
    top = ranked[-1]
    return "amber" if top == "orange" else top


def assess(
    topic: str, published_at: datetime | None, score: float, domain: str, text: str, now: datetime
) -> Assessed:
    found = Assessed()
    _, _, oldest = TOPICS[topic]
    if published_at is None:
        found.flag("undated", "The provider could not tell when it was published")
    elif now - published_at > oldest:
        days = (now - published_at).total_seconds() / 86400
        found.flag(
            "stale",
            f"Published {days:.1f} days ago, older than "
            f"{oldest.total_seconds() / 86400:g} days for {topic.replace('_', ' ')}",
        )
    if score < LOW_SCORE and not under(domain, TRUSTED):
        found.flag(
            "low_confidence",
            f"A weak match, {score:.2f}, from a domain not on the authoritative list",
        )
    if screen(text):
        found.flag("instructions_to_ai", "Its text addresses an AI. It is only text")
    return found


def flag_conflicts(levels: list[str], assessed: list[Assessed]) -> None:
    """Flag every source naming a warning level when the sources name more than one."""
    named = {level for level in levels if level}
    if len(named) < 2:
        return
    for level, one in zip(levels, assessed, strict=True):
        if level:
            others = ", ".join(sorted(named - {level}))
            one.flag("conflicting", f"It gives a {level} warning; other sources give {others}")
