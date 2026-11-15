"""The AI governance picture, its page and the tracker stay matched."""

import re
from pathlib import Path

import pytest

from scripts.generate.governance_view import GAP, PLANNED, PROPOSED, THEMES, Answer, status

ROOT = Path(__file__).resolve().parent.parent
TRACKER = (ROOT / "docs" / "phases" / "README.md").read_text()
PAGE = (ROOT / "docs" / "ai-governance.md").read_text()


def test_every_cited_module_is_in_the_tracker() -> None:
    for theme in THEMES:
        for answer in theme.answers:
            status(answer, TRACKER)  # Raises for a module the tracker lacks.


def test_a_control_is_in_place_only_once_all_its_modules_are_done() -> None:
    tracker = "- [x] **M1.1 One:** done.\n- [ ] **M1.2 Two:** not yet.\n"
    assert status(Answer("a", "M1.1"), tracker) == "in place"
    assert status(Answer("b", "M1.1, M1.2"), tracker) == PLANNED
    assert status(Answer("c", GAP), tracker) == PROPOSED
    with pytest.raises(ValueError, match="not in the tracker"):
        status(Answer("d", "M9.9"), tracker)


def test_every_theme_has_a_section_on_the_page() -> None:
    headings = set(re.findall(r"^### (.+)$", PAGE, re.M))
    assert {theme.name for theme in THEMES} == headings


def test_the_readme_shows_the_picture_and_links_the_page() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "docs/assets/ai-governance.png" in readme
    assert "docs/ai-governance.md" in readme
    if (ROOT / "docs/assets").is_dir():
        assert (ROOT / "docs/assets/ai-governance.png").is_file()
