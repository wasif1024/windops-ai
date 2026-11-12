#!/usr/bin/env python3
"""Check the Phase 4 exit gate on the real local cluster.

SRS section 12: a seeded fault profile streams into storage with the correct quality labels.
`make fault-stream-check` proves that directly, against the simulator's hidden ground truth. It
spends most of its time waiting for 10-minute periods to close, so it runs alongside the others:
every Phase 4 check, and the route sweep, which covers the fleet screens' routes. It uses its own
turbine, so nothing the others do touches its data. Each check's output goes to its own log.
Every check runs, so one failure does not hide another.

Run `make dev` once first, or the setup in infrastructure/minikube/README.md. It takes about 25
minutes.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
# (make target, what it proves for the gate, whether it signs in through the gateway)
ALONGSIDE = (
    "fault-stream-check",
    "a seeded fault profile streams into storage with the correct quality labels",
)
CHECKS = (
    ("asset-check", "wind farms, turbines and components, isolated by tenant", False),
    ("telemetry-check", "device credentials, the device route and storage", False),
    ("telemetry-storage-check", "partitions, 10-minute summaries, late data and retention", False),
    ("cms-check", "CMS vibration features, a worn bearing's signature and raw windows", False),
    ("data-quality-check", "one of each data problem, sent and labelled", False),
    ("snapshot-check", "telemetry read back, and frozen into reproducible snapshots", False),
    ("freshness-check", "the Data Freshness dashboard and its alerts", False),
    # After the CMS check: restarting the simulator zeroes its CMS counts, sent every 10 minutes.
    ("simulator-check", "the simulator streams the demo fleet, and survives a restart", False),
    ("route-sweep", "every route, the fleet screens' too, refuses who it must", False),
    ("frontend-check", "the fleet screens in a real browser, phone and desktop", True),
)
# The gateway allows 5 sign-ins a minute from one address (SRS 7.5).
SIGN_IN_WINDOW_SECONDS = 61


def run(target: str, logs: Path) -> tuple[bool, int, Path]:
    log = logs / f"{target}.log"
    started = time.monotonic()
    with log.open("w") as output:
        result = subprocess.run(
            ["make", "--no-print-directory", target],
            cwd=REPOSITORY, stdout=output, stderr=subprocess.STDOUT, check=False,
        )  # fmt: skip
    return result.returncode == 0, int(time.monotonic() - started), log


def report(target: str, proves: str, passed: bool, seconds: int, log: Path) -> bool:
    if passed:
        print(f"ok - {target} ({seconds} s): {proves}", flush=True)
    else:
        print(f"not ok - {target} ({seconds} s): {proves}. See {log}", flush=True)
    return passed


def main() -> int:
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-4-gate-"))
    print(f"Logs: {logs}", flush=True)
    failed: list[str] = []
    started = time.monotonic()
    target, proves = ALONGSIDE
    alongside_log = logs / f"{target}.log"
    with alongside_log.open("w") as output:
        alongside = subprocess.Popen(
            ["make", "--no-print-directory", target],
            cwd=REPOSITORY, stdout=output, stderr=subprocess.STDOUT,
        )  # fmt: skip
    print(f"info - {target} started alongside the other checks", flush=True)
    for target, proves, signs_in in CHECKS:
        if signs_in:
            time.sleep(SIGN_IN_WINDOW_SECONDS)
        passed, seconds, log = run(target, logs)
        if not report(target, proves, passed, seconds, log):
            failed.append(target)
    target, proves = ALONGSIDE
    code = alongside.wait()
    if not report(target, proves, code == 0, int(time.monotonic() - started), alongside_log):
        failed.append(target)
    minutes = int((time.monotonic() - started) // 60)
    if failed:
        print(
            f"Phase 4 exit gate failed in {minutes} minutes: {', '.join(failed)}", file=sys.stderr
        )
        return 1
    print(f"Phase 4 exit gate passed in {minutes} minutes: all {len(CHECKS) + 1} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
