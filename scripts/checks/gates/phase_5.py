#!/usr/bin/env python3
"""Check the Phase 5 exit gate on the real local cluster.

SRS section 12: a fault creates one deduplicated case bound to an immutable snapshot.
`make fault-case-check` proves that directly: a seeded gearbox fault, through the gateway, found by
the default rules, deduplicated into one case, with evidence PostgreSQL will not change. It spends
most of its time streaming the fault live, so it runs alongside the others: every Phase 5 check,
and the earlier checks covering what Phase 5 changed. Those are the shared gRPC and identity
libraries, Authorization's service grants, the Telemetry Service's new events and snapshot holds,
and Web BFF's alarm routes.

The Phase 1 and 2 gates restart NATS and break a call on purpose, and the mutual TLS check renews
a certificate, so they run after the fault has streamed, not during it. Each check's output goes
to its own log, and every check runs, so one failure does not hide another.

Run `make dev` once first, or the setup in infrastructure/minikube/README.md. It takes about 50
minutes.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
ALONGSIDE = (
    "fault-case-check",
    "a fault creates one deduplicated case bound to an immutable snapshot",
)
# (make target, what it proves for the gate, whether it signs in through the gateway)
DURING = (
    ("edge-check", "the gateway serves only HTTPS, and only Keycloak's sign-in pages", False),
    ("alarm-check", "a device raises an alarm, people act on it, the device clears it", False),
    ("fault-rules-check", "rules versioned, audited and changed only by a person", False),
    ("detection-check", "the detector finds, freezes its evidence, publishes and closes", False),
    ("case-check", "two rules make one case, with evidence; cancelled; a person's own", False),
    ("telemetry-check", "device credentials, the device route and storage", False),
    ("snapshot-check", "telemetry read back, and frozen into held snapshots", False),
    ("authorization-check", "roles, scopes, break-glass and audited denials", False),
    ("audit-check", "a tamper-evident audit record", False),
    ("cross-tenant-check", "every data path attacked across tenants, and refused", False),
    ("security-check", "the Security dashboard and alerts, from real refusals", False),
    ("route-sweep", "every route, the alarm screens' too, refuses who it must", False),
    ("bff-check", "Web BFF with a real login through the gateway", True),
    ("frontend-check", "every screen in a real browser, the alarm console too", True),
)
AFTER = (
    ("mtls-check", "mutual TLS, with a real certificate renewal", False),
    ("phase-1-gate", "a traced call through the gateway, and its breaker", False),
    ("phase-2-gate", "an event survives a NATS restart, and redelivery makes one record", False),
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


def run_all(checks: tuple[tuple[str, str, bool], ...], logs: Path, failed: list[str]) -> None:
    for target, proves, signs_in in checks:
        if signs_in:
            time.sleep(SIGN_IN_WINDOW_SECONDS)
        passed, seconds, log = run(target, logs)
        if not report(target, proves, passed, seconds, log):
            failed.append(target)


def main() -> int:
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-5-gate-"))
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
    run_all(DURING, logs, failed)
    code = alongside.wait()
    if not report(target, proves, code == 0, int(time.monotonic() - started), alongside_log):
        failed.append(target)
    run_all(AFTER, logs, failed)
    minutes = int((time.monotonic() - started) // 60)
    total = len(DURING) + len(AFTER) + 1
    if failed:
        print(
            f"Phase 5 exit gate failed in {minutes} minutes: {', '.join(failed)}", file=sys.stderr
        )
        return 1
    print(f"Phase 5 exit gate passed in {minutes} minutes: all {total} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
