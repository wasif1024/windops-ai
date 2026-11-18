"""What goes to the provider: generic research words, never the tenant's details (EXT-006).

The caller's words go out only after everything that names the tenant is taken out of them:

- the tenant's ID and name;
- its wind farms' and turbines' IDs and names, and the turbines' serial numbers;
- any platform record ID, such as a case's, and e-mail addresses and web addresses.

Each is taken out whole, matched without regard to case, and only the kind of thing it was is
recorded, never the thing. A credential refuses the query outright. A site goes as its approximate
region, and a turbine as its generic class, never its model.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from windops_screening import credentials_in

MAXIMUM_QUERY = 400
# Any platform record's ID: a short prefix, then its random part, such as case_ or fct_.
_RECORD_ID = re.compile(r"\b[a-z]{2,5}_[a-z0-9]{12,32}\b", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_ADDRESS = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
# Region names for the countries the platform serves, as Tavily's country filter spells them.
COUNTRIES = {
    "GB": "united kingdom",
    "IE": "ireland",
    "ES": "spain",
    "PT": "portugal",
    "FR": "france",
    "DE": "germany",
    "NL": "netherlands",
    "BE": "belgium",
    "DK": "denmark",
    "NO": "norway",
    "SE": "sweden",
    "FI": "finland",
    "PL": "poland",
    "IT": "italy",
    "GR": "greece",
    "SA": "saudi arabia",
    "AE": "united arab emirates",
    "EG": "egypt",
    "MA": "morocco",
    "US": "united states",
    "CA": "canada",
    "BR": "brazil",
    "AU": "australia",
    "IN": "india",
    "CN": "china",
    "JP": "japan",
    "KR": "south korea",
    "TW": "taiwan",
    "ZA": "south africa",
}


class QueryRefusedError(ValueError):
    """The query cannot go out, and why."""


@dataclass(frozen=True)
class Identifiers:
    """Everything in the tenant that names it, a site, a turbine or a person."""

    tenant: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    turbines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outbound:
    query: str
    redactions: tuple[str, ...]


@dataclass(frozen=True)
class Site:
    country: str
    latitude: float
    longitude: float
    offshore: bool


def region(site: Site) -> str:
    """Approximate: the sea or land, the country, and the nearest whole degree."""
    north = f"{abs(round(site.latitude))}°{'N' if site.latitude >= 0 else 'S'}"
    east = f"{abs(round(site.longitude))}°{'E' if site.longitude >= 0 else 'W'}"
    country = COUNTRIES.get(site.country, site.country).title()
    return f"{'offshore' if site.offshore else 'onshore'}, {country}, near {north} {east}"


def turbine_class(rated_power_kw: float, offshore: bool) -> str:
    """The generic class, never the model: how big, and where it stands."""
    return f"{rated_power_kw / 1000:g} MW {'offshore' if offshore else 'onshore'} wind turbine"


def _names(values: Iterable[str]) -> list[str]:
    # Longest first, so "Dogger East T01" goes before "Dogger East" can split it.
    return sorted({v.strip() for v in values if len(v.strip()) >= 3}, key=len, reverse=True)


def _take_out(text: str, values: Iterable[str]) -> tuple[str, bool]:
    found = False
    for value in _names(values):
        # Whole words only, and a hyphenated ID also as words, as "north-sea" or "north sea".
        for form in {value, value.replace("-", " ")}:
            pattern = re.compile(rf"(?<![\w-]){re.escape(form)}(?![\w-])", re.IGNORECASE)
            text, count = pattern.subn(" ", text)
            found = found or count > 0
    return text, found


def compose(
    query: str, identifiers: Identifiers, *, region_text: str = "", class_text: str = ""
) -> Outbound:
    """The query that goes out, and what kinds of thing were taken out of it."""
    words = query.strip()
    if not words:
        raise QueryRefusedError("A query is needed")
    if len(words) > MAXIMUM_QUERY:
        raise QueryRefusedError(f"A query is at most {MAXIMUM_QUERY} characters")
    found = credentials_in(words)
    if found:
        raise QueryRefusedError(f"The query holds {found[0][0]}, so it is not sent. Remove it")
    redactions = []
    for kind, pattern in (("record_id", _RECORD_ID), ("email", _EMAIL), ("address", _ADDRESS)):
        words, count = pattern.subn(" ", words)
        if count:
            redactions.append(kind)
    for kind, values in (
        ("turbine", identifiers.turbines),
        ("site", identifiers.sites),
        ("tenant", identifiers.tenant),
    ):
        words, removed = _take_out(words, values)
        if removed:
            redactions.append(kind)
    words = _SPACE.sub(" ", words).strip(" ,;:")
    if len(re.findall(r"\w{3,}", words)) == 0:
        raise QueryRefusedError(
            "Nothing generic is left of the query once the tenant's details are taken out"
        )
    sent = " ".join(part for part in (words, class_text, region_text) if part)
    return Outbound(sent[:MAXIMUM_QUERY].strip(), tuple(redactions))
