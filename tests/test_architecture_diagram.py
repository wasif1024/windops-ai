"""Keep the security diagram true to the tracker, so it cannot quietly go stale."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from generate_readme_visuals import (
    AGENT_SERVICE,
    APPLICATION_GROUPS,
    ENTRY_SERVICES,
    all_controls,
    read_phase_progress,
)

ROOT = Path(__file__).resolve().parent.parent
TRACKER = (ROOT / "docs" / "phases" / "README.md").read_text()


def module_state(module: str) -> str | None:
    found = re.search(rf"^- \[([ x])\] \*\*{re.escape(module)} ", TRACKER, re.M)
    return found.group(1) if found else None


def planned_modules() -> list[tuple[str, str]]:
    return [
        (control.text, module.strip())
        for control in all_controls()
        if control.module
        for module in control.module.split(",")
    ]


def test_every_cited_module_exists_in_the_tracker() -> None:
    for text, module in planned_modules():
        assert module_state(module) is not None, f"{text}: {module} is not in the tracker"


def test_a_control_is_not_shown_as_planned_once_its_modules_are_done() -> None:
    """When this fails, mark the control as in place and run the visuals script."""
    by_control: dict[str, list[str | None]] = {}
    for text, module in planned_modules():
        by_control.setdefault(text, []).append(module_state(module))
    for text, states in by_control.items():
        assert any(state == " " for state in states), f"{text} is finished, so update the diagram"


def test_roadmap_reads_all_phases_with_the_tracker_totals() -> None:
    phases = read_phase_progress()
    assert [number for number, _, _, _ in phases] == list(range(1, 16))
    table = dict(re.findall(r"^\| (\d+) \| \[[^\]]+\]\([^)]*\) \| (\d+) \|", TRACKER, re.M))
    for number, _, _, total in phases:
        assert int(table[str(number)]) == total


def test_roadmap_progress_counts_ticked_modules(tmp_path: Path) -> None:
    tracker = tmp_path / "README.md"
    tracker.write_text("## Phase 1: First\n\n- [x] a\n- [ ] b\n\n## Phase 2: Second\n\n- [ ] c\n")
    assert read_phase_progress(tracker) == [(1, "First", 1, 2), (2, "Second", 0, 1)]


def test_readme_shows_the_diagram() -> None:
    assert "docs/assets/security-layers.png" in (ROOT / "README.md").read_text()


def test_readme_shows_the_end_to_end_diagram() -> None:
    assert "docs/assets/end-to-end-architecture.png" in (ROOT / "README.md").read_text()


def test_end_to_end_diagram_draws_every_srs_service_once() -> None:
    srs = (ROOT / "docs" / "srs.md").read_text()
    table = srs[srs.index("### 7.2 Services") : srs.index("### 7.3")]
    names = re.findall(r"^\| ([A-Z][^|]+?) \|", table, re.M)
    expected = sorted(name.removesuffix(" (new)") for name in names if name != "Service")
    drawn = [*ENTRY_SERVICES, AGENT_SERVICE]
    drawn += [name for _, _, members in APPLICATION_GROUPS for name, _ in members]
    assert sorted(drawn) == expected
