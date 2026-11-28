"""Every SRS 7.7 breaker and fallback has a fault-injection test, or a module to build it.

RES-005.
"""

import ast
import re
from functools import cache
from pathlib import Path

import pytest

from scripts.checks.resilience.fallbacks import BREAKER_TESTS, FALLBACKS, all_tests

ROOT = Path(__file__).resolve().parents[1]
SRS = ROOT / "docs" / "srs.md"
TRACKER = ROOT / "docs" / "phases" / "README.md"


def srs_rows() -> list[str]:
    """The dependencies in SRS 7.7's "Dependency down" table, in order."""
    lines = SRS.read_text().splitlines()
    start = lines.index("| Dependency down | Defined behavior when the breaker opens |")
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line.split("|")[1].strip())
    return rows


@cache
def functions(path: str) -> frozenset[str]:
    tree = ast.parse((ROOT / path).read_text())
    return frozenset(
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def module_done(module: str) -> bool:
    match = re.search(rf"^- \[(.)\] \*\*{re.escape(module)} ", TRACKER.read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{module} is not a module in the tracker")
    return match.group(1) == "x"


def test_every_row_of_the_table_has_an_entry_in_order() -> None:
    assert [entry.dependency for entry in FALLBACKS] == srs_rows()


def test_every_entry_has_tests_or_names_the_module_building_it() -> None:
    for entry in FALLBACKS:
        assert entry.tests or entry.gap, f"{entry.dependency} has neither tests nor a module"
        if entry.gap:
            assert entry.note, f"{entry.dependency} must say what is missing"


def test_a_gap_belongs_to_a_module_not_yet_done() -> None:
    """Once its module is ticked, a gap must be replaced by the tests that close it."""
    for entry in FALLBACKS:
        if entry.gap:
            assert not module_done(entry.gap), (
                f"{entry.gap} is done, so {entry.dependency} needs its tests, not a gap"
            )


@pytest.mark.parametrize("test", all_tests())
def test_each_named_test_exists(test: str) -> None:
    path, _, name = test.partition("::")
    assert (ROOT / path).is_file(), f"No file {path}"
    assert name in functions(path), f"No test {name} in {path}"


def test_the_breaker_itself_is_covered() -> None:
    names = {test.partition("::")[2] for test in BREAKER_TESTS}
    for behavior in ("opens", "refuses", "half_open", "close", "reopens", "not_retried"):
        assert any(behavior in name for name in names), f"No breaker test for {behavior}"
