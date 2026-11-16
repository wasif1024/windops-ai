#!/usr/bin/env python3
"""Run every Phase 6 boundary test, and pass only if all of them pass (M6.5).

The suites: the boundary cases generated from the default libraries, the rule engine's own tests,
and the impact formulas'. A skipped or erroring test counts against the 100%, as a failing one does.
"""

import re
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
SUITES = (
    "tests/test_phase_6_boundaries.py",
    "services/policy-service/tests/test_rules.py",
    "services/production-impact-service/tests/test_formulas.py",
)


def main() -> int:
    result = subprocess.run(
        ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", *SUITES],
        cwd=REPOSITORY, capture_output=True, text=True, check=False,
    )  # fmt: skip
    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    counts = {
        kind: int(n) for n, kind in re.findall(r"(\d+) (passed|failed|error|skipped)", summary)
    }
    passed, total = counts.get("passed", 0), sum(counts.values())
    if result.returncode != 0 or total == 0 or passed != total:
        print(result.stdout[-4000:], file=sys.stderr)
        print(f"Boundary check failed: {passed} of {total} passed", file=sys.stderr)
        return 1
    print(f"ok - {total} boundary tests in {len(SUITES)} suites, 100% passed")
    print("Boundary check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
