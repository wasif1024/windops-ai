#!/usr/bin/env python3
"""Check the Phase 10 exit gate on the real local cluster.

SRS section 12: the full flow passes from fault to verification, with 0 approval bypasses.
`make full-flow-check` proves it directly: a seeded fault on a turbine of its own is detected,
investigated, proposed, approved by a person with no hand in it, done, verified and closed, and
every way around the approval tried along the way is refused.

Around it run every Phase 10 check, and the earlier checks covering what Phase 10 changed: the
maintenance and agent-denial checks, as work orders gained approval, staleness and completion;
the console, with its approval queue; Web BFF, with notifications; Authorization, with approval
policy changes and permission holders; isolation across tenants, now attacking the Approval and
Notification databases; the audit trail and JetStream, with the new subjects and consumers; and
the migration Jobs. Each check's output goes to its own log, and every check runs, so one
failure does not hide another.

It spends nothing: every agent run is rehearsed, and email is only recorded.

**The node's load:** on the 6-CPU local node, a check that starts while the last is still settling
can starve a service of CPU until its probes fail and it restarts, failing whatever runs then. So
before each check the gate waits, up to ten minutes, until every pod is ready and the node's load
is below 24, four for each CPU, and says when that did not happen. The streaming fleet alone keeps
the load at about 15.

Name checks to run only those, as after a failure that was the cluster's, not the code's:
`uv run python -m scripts.checks.gates.phase_10 approval-check stale-check`.

Run `make dev` once first, then the seeds, with WINDOPS_INVESTIGATION_AGENTS=rehearse and
WINDOPS_VERIFICATION_WINDOW_MINUTES=2. About 90 minutes.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
CALM_LOAD, CALM_WAIT_SECONDS = 24.0, 600
# (make target and its variables, what it proves for the gate)
CHECKS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("full-flow-check",),
     "a fault to verification, with 0 approval bypasses"),
    (("approval-check",),
     "only a person decides, on what is current, apart from their own work, failing closed"),
    (("policy-change-check",), "a change to who may approve waits for a second administrator"),
    (("stale-check",), "versioned stale rules, shown at approval, re-evaluated as new versions"),
    (("stale-watch-check",), "condition drift and weather, polled"),
    (("resume-check",), "a decision resumes the case's workflow from its checkpoint"),
    (("notification-check",), "who is told what, in the app and by recorded email, no evidence"),
    (("verification-check",), "completed work verified by the rules, and a person closes the case"),
    (("maintenance-check",), "work orders validated, drafted by anyone, submitted by a person"),
    (("agent-denial-check",), "an agent drafts, and never submits or reserves"),
    (("frontend-check",), "every console journey, the approval queue included, on a phone too"),
    (("bff-check",), "Web BFF's routes, notifications included"),
    (("authorization-check",), "roles, scopes, service grants and audited denials"),
    (("cross-tenant-check",), "every service's database and API attacked across tenants"),
    (("audit-check",), "a tamper-evident audit record"),
    (("migrate-check",), "migration Jobs run once per release"),
    # Last: it restarts NATS, and the services take a while to reconnect.
    (("nats-check",), "JetStream keeps events and limits each publisher"),
)  # fmt: skip


def load() -> float:
    """The node's one-minute load average, or 0 when it cannot be read."""
    found = subprocess.run(["minikube", "-p", "windops", "ssh", "--", "cat", "/proc/loadavg"],
                           capture_output=True, text=True, check=False)  # fmt: skip
    try:
        return float(found.stdout.split()[0])
    except (IndexError, ValueError):
        return 0.0


def unready() -> int:
    """How many pods are not running and ready, as a restart or a crash leaves them."""
    found = subprocess.run(["kubectl", "--context", "windops", "get", "pods", "-A", "--no-headers"],
                           capture_output=True, text=True, check=False)  # fmt: skip
    count = 0
    for line in found.stdout.splitlines():
        fields = line.split()
        # Observability may be paused to spare the node; its log shipper is then never ready.
        if len(fields) < 4 or fields[3] == "Completed" or fields[0] == "windops-observability":
            continue
        ready, _, wanted = fields[2].partition("/")
        count += fields[3] != "Running" or ready != wanted
    return count


def calm() -> str:
    """Wait for the node to settle. Returns a note when it did not in time."""
    deadline = time.monotonic() + CALM_WAIT_SECONDS
    while True:
        now, waiting = load(), unready()
        if now < CALM_LOAD and not waiting:
            return ""
        if time.monotonic() > deadline:
            return f" (started at load {now:.1f}, {waiting} pods unready, after waiting)"
        time.sleep(20)


def run(target: tuple[str, ...], logs: Path) -> tuple[bool, int, Path]:
    log = logs / f"{target[0]}.log"
    started = time.monotonic()
    with log.open("w") as output:
        result = subprocess.run(
            ["make", "--no-print-directory", *target],
            cwd=REPOSITORY, stdout=output, stderr=subprocess.STDOUT, check=False,
        )  # fmt: skip
    return result.returncode == 0, int(time.monotonic() - started), log


def main(only: list[str]) -> int:
    unknown = set(only) - {target[0] for target, _ in CHECKS}
    if unknown:
        print(f"Not a gate check: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    chosen = [(t, proves) for t, proves in CHECKS if not only or t[0] in only]
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-10-gate-"))
    print(f"Logs: {logs}", flush=True)
    failed: list[str] = []
    started = time.monotonic()
    for target, proves in chosen:
        note = calm()
        passed, seconds, log = run(target, logs)
        name = " ".join(target) + note
        if passed:
            print(f"ok - {name} ({seconds} s): {proves}", flush=True)
        else:
            print(f"not ok - {name} ({seconds} s): {proves}. See {log}", flush=True)
            failed.append(name)
    minutes = int((time.monotonic() - started) // 60)
    if failed:
        print(f"Phase 10 exit gate failed in {minutes} minutes: {', '.join(failed)}",
              file=sys.stderr)  # fmt: skip
        return 1
    if only:
        print(f"The {len(chosen)} checks named passed in {minutes} minutes")
        return 0
    print(f"Phase 10 exit gate passed in {minutes} minutes: all {len(CHECKS)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
