"""Keep the service map complete against the SRS, so it cannot quietly miss a service."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRS = (ROOT / "docs" / "srs.md").read_text()
MAP = (ROOT / "docs" / "service-map.md").read_text()


def section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


def first_column(text: str) -> list[str]:
    rows = re.findall(r"^\| ([^|]+?) \|", text, re.M)
    return [row for row in rows if not set(row) <= {"-"}]


def srs_services() -> list[str]:
    rows = first_column(section(SRS, "### 7.2 Services", "### 7.3"))
    return [name.removesuffix(" (new)") for name in rows if name != "Service"]


def test_srs_lists_the_23_services() -> None:
    assert len(srs_services()) == 23


@pytest.mark.parametrize("service", srs_services())
def test_every_service_has_a_row_of_links(service: str) -> None:
    table = section(MAP, "## Every service and its links", "The telemetry simulator")
    assert service in first_column(table)


@pytest.mark.parametrize("service", srs_services())
def test_every_service_is_in_the_diagram(service: str) -> None:
    """The gateway and the rest may use a short node ID, but must show their full name."""
    diagram = MAP[MAP.index("```mermaid") : MAP.index("## Every service and its links")]
    assert service in diagram


@pytest.mark.parametrize(
    "subject",
    re.findall(r"`([a-z]+\.[a-z]+)`", section(SRS, "Initial subjects are", "### 7.7")),
)
def test_every_srs_event_has_a_publisher_and_a_listener(subject: str) -> None:
    table = section(MAP, "## Every service and its links", "The telemetry simulator")
    rows = [line.split("|") for line in table.splitlines() if line.startswith("| ")]
    publishers = [row for row in rows if f"`{subject}`" in row[3]]
    listeners = [row for row in rows if f"`{subject}`" in row[4]]
    assert publishers, f"{subject} has no publisher"
    # Audit's `audit.recorded` is a confirmation for later consumers, so nothing needs it yet.
    assert listeners or subject == "audit.recorded", f"{subject} has no listener"


def test_every_defined_fallback_matches_the_srs() -> None:
    srs_table = section(SRS, "| Dependency down |", "- **RES-001:**")
    map_table = section(MAP, "### Defined by SRS 7.7", "### Proposed")
    assert [line for line in srs_table.splitlines() if line.startswith("| ")] == [
        line for line in map_table.splitlines() if line.startswith("| ")
    ]


def test_proposed_fallbacks_do_not_repeat_a_defined_one() -> None:
    defined = set(first_column(section(MAP, "### Defined by SRS 7.7", "### Proposed")))
    proposed = first_column(section(MAP, "### Proposed", "## Where to read more"))
    assert not (defined - {"Dependency down"}) & set(proposed)
