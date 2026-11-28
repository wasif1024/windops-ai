#!/usr/bin/env python3
"""Inject every SRS 7.7 fault, and check each breaker and fallback behaves as defined (RES-005).

`make chaos` runs every fault-injection test in fallbacks.py, against the real code of each
service with its dependency taken down, and prints the SRS 7.7 table with the result for each
row. A row whose behavior is not built yet shows the module that builds it, and is not counted
as passing. It needs no cluster.
"""

import subprocess
import sys

from scripts.checks.resilience.fallbacks import BREAKER_TESTS, FALLBACKS, all_tests


def count(number: int) -> str:
    return f"{number} test" if number == 1 else f"{number} tests"


def main() -> int:
    tests = all_tests()
    print(f"Injecting faults: {len(tests)} tests, {len(FALLBACKS)} dependencies", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *tests],
        check=False,
    )
    passed = result.returncode == 0
    mark = "ok" if passed else "FAILED"
    print(f"\n{mark:>8}  The breaker itself, {count(len(BREAKER_TESTS))}")
    gaps = 0
    for entry in FALLBACKS:
        if entry.gap:
            gaps += 1
            built = f", partly: {count(len(entry.tests))}" if entry.tests else ""
            print(f"{'GAP':>8}  {entry.dependency}: built by {entry.gap}{built}. {entry.note}")
        else:
            print(f"{mark:>8}  {entry.dependency}, {count(len(entry.tests))}")
    if not passed:
        print("\nA fault-injection test failed", file=sys.stderr)
        return 1
    if gaps:
        print(f"\nEvery built fallback passed. {gaps} wait for the module that builds them.")
    else:
        print("\nEvery SRS 7.7 breaker and fallback passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
