"""The Phase 11 gate's coverage rule, on made-up Playwright reports (M11.8)."""

import json
from typing import Any

from scripts.checks.consoles.coverage import MENUS, PROJECTS, check, srs_screens


def report(*runs: tuple[str, str, bool, str, list[str]]) -> dict[str, Any]:
    """(title, project, passed, role, screens) for each journey run."""
    specs = [{
        "title": title,
        "tests": [{
            "projectName": project, "status": "expected" if passed else "unexpected",
            "annotations": [{"type": "role", "description": role},
                            *({"type": "screen", "description": s} for s in screens)],
            "results": [],
        }],
    } for title, project, passed, role, screens in runs]  # fmt: skip
    return {"suites": [{"title": "e2e", "specs": [], "suites": [{"specs": specs}]}]}


def everything() -> list[tuple[str, str, bool, str, list[str]]]:
    screens = list(srs_screens())
    roles = list(json.loads(MENUS.read_text()))
    return [(f"journey as {role}", project, True, role, screens if i == 0 else [])
            for i, role in enumerate(roles) for project in PROJECTS]  # fmt: skip


def test_the_list_is_every_srs_4_11_screen() -> None:
    screens = srs_screens()
    assert len(screens) == 29
    assert screens["evidence-viewer"] == "Evidence viewer"
    assert screens["platform-branding-domains"].startswith("Platform console")


def test_it_passes_only_with_every_screen_on_both_sizes_and_every_role() -> None:
    assert check(report(*everything())) == []
    no_phone = [r for r in everything() if not (r[1] == "phone" and r[4])]
    assert any("on phone" in p for p in check(report(*no_phone)))
    no_auditor = [r for r in everything() if r[3] != "auditor"]
    assert "No passing journey signs in as auditor" in check(report(*no_auditor))
    failing = [(t, p, False if p == "desktop" and i == 0 else ok, role, s)
               for i, (t, p, ok, role, s) in enumerate(everything())]  # fmt: skip
    problems = check(report(*failing))
    assert any("failed on desktop" in p for p in problems)
    assert any("no passing journey on desktop" in p for p in problems)
