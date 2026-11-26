#!/usr/bin/env python3
"""The Phase 11 gate's own test: role-based browser tests cover every SRS 4.11 screen (M11.8).

Reads the JSON report of the last `make frontend-check`. Each journey says, in its annotations,
the role it signed in as and the SRS 4.11 screens it covered (frontend/e2e/coverage.ts), and
checks its role's menu against frontend/e2e/role-menus.json as it signs in. This passes only when:

- every journey passed, on every screen size;
- every SRS 4.11 screen was covered by a passing journey on a desktop and on a phone (UX-001);
- every SRS 3.2 role signed in to at least one passing journey, its menu exactly its screens.

    make frontend-check && uv run python -m scripts.checks.consoles.coverage
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
REPORT = FRONTEND / "playwright-report" / "results.json"
COVERAGE = FRONTEND / "e2e" / "coverage.ts"
MENUS = FRONTEND / "e2e" / "role-menus.json"
PROJECTS = ("desktop", "phone")


def srs_screens() -> dict[str, str]:
    """The SRS 4.11 screens, by ID, as coverage.ts lists them."""
    text = COVERAGE.read_text()
    body = text[text.index("export const SRS_4_11") : text.index("} as const")]
    return dict(re.findall(r'"?([a-z][a-z0-9-]*)"?:\s*"([^"]+)"', body))


def journeys(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every journey run: its title, screen size, whether it passed, its role and screens."""
    found: list[dict[str, Any]] = []

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                notes = [*test.get("annotations", [])]
                for result in test.get("results", []):
                    notes += result.get("annotations", [])
                found.append({
                    "title": spec["title"], "project": test.get("projectName", ""),
                    "passed": test.get("status") == "expected",
                    "role": next((n["description"] for n in notes if n["type"] == "role"), ""),
                    "screens": {n["description"] for n in notes if n["type"] == "screen"},
                })  # fmt: skip
        for child in suite.get("suites", []):
            walk(child)

    for suite in report.get("suites", []):
        walk(suite)
    return found


def check(report: dict[str, Any]) -> list[str]:
    """What fails the gate, in words; empty when it passes."""
    runs = journeys(report)
    problems = [f"{r['title']} failed on {r['project']}" for r in runs if not r["passed"]]
    problems += [f"{r['title']} names no role" for r in runs if not r["role"]]
    covered: dict[str, set[str]] = defaultdict(set)
    roles: set[str] = set()
    for run in runs:
        if run["passed"]:
            roles.add(run["role"])
            for screen in run["screens"]:
                covered[screen].add(run["project"])
    for screen, name in srs_screens().items():
        missing = [p for p in PROJECTS if p not in covered[screen]]
        if missing:
            problems.append(f"{name} has no passing journey on {' or '.join(missing)}")
    for role in json.loads(MENUS.read_text()):
        if role not in roles:
            problems.append(f"No passing journey signs in as {role}")
    return problems


def main() -> int:
    if not REPORT.exists():
        print(f"No report at {REPORT}. Run: make frontend-check", file=sys.stderr)
        return 1
    report = json.loads(REPORT.read_text())
    runs = journeys(report)
    screens = srs_screens()
    for screen_id, name in screens.items():
        by = sorted({r["role"] for r in runs if r["passed"] and screen_id in r["screens"]})
        on = sorted({r["project"] for r in runs if r["passed"] and screen_id in r["screens"]})
        mark = "ok" if set(on) >= set(PROJECTS) else "not ok"
        print(f"{mark} - {name}: {', '.join(by) or 'no role'} ({', '.join(on) or 'no size'})")
    problems = check(report)
    if problems:
        for problem in problems:
            print(f"not ok - {problem}", file=sys.stderr)
        return 1
    roles = sorted({r["role"] for r in runs})
    print(
        f"All {len(screens)} SRS 4.11 screens are covered on a desktop and a phone, by "
        f"{len(runs)} passing journeys as {len(roles)} roles, each menu exactly its screens"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
