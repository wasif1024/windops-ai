#!/usr/bin/env python3
"""Check the Phase 11 exit gate on the real local cluster.

SRS section 12: role-based end-to-end browser tests cover every Section 4.11 screen. The gate is
`make frontend-check`, every console journey through the gateway on a desktop and a phone, then
`scripts.checks.consoles.coverage`, which passes only when every SRS 4.11 screen was covered by a
passing journey on both, and every SRS 3.2 role signed in, its menu exactly its screens.

Around it run the checks for what Phase 11 changed in the services: the Knowledge Service's
browsing (M11.5), the audit trail's filters and index migration and the notification rules
(M11.6), the approval queue's standing (M11.4), Web BFF's routes, impact and weather (M11.3), and
isolation across tenants. Every check runs, so one failure does not hide another.

It spends nothing on Claude: every investigation is rehearsed. The knowledge search makes one
OpenAI embedding call per screen size.

**The node:** as for the Phase 10 gate, it waits for a calm node before each check, and it pauses
the observability stack for the run, which starved the services of CPU in M11.5, restoring it at
the end. Name checks to run only those: `uv run python -m scripts.checks.gates.phase_11 bff-check`.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.checks.gates.phase_10 import calm, run

OBSERVABILITY = (
    "deployment/windops-alertmanager", "deployment/windops-grafana",
    "deployment/windops-kube-state-metrics", "statefulset/windops-loki",
    "statefulset/windops-prometheus", "statefulset/windops-tempo",
)  # fmt: skip
# (make target, what it proves for the gate)
CHECKS: tuple[tuple[str, str], ...] = (
    ("frontend-check", "every console journey, as each role, on a desktop and a phone"),
    ("browser-coverage-check", "every SRS 4.11 screen covered on both, and every role's menu"),
    ("knowledge-check", "the library browsed within permissions, uploads screened and held"),
    ("retrieval-check", "search and citations, scoped by wind farm, across revisions"),
    ("audit-check", "a tamper-evident audit record, now filtered for the explorer"),
    ("notification-check", "who is told what, read in the notification center"),
    ("approval-check", "the queue's standing, and only a person decides on what is current"),
    ("impact-check", "the production impact figures the impact view shows"),
    ("weather-check", "the forecast and its freshness the weather panel shows"),
    ("bff-check", "Web BFF's routes"),
    ("cross-tenant-check", "every service's database and API attacked across tenants"),
    ("migrate-check", "migration Jobs, the audit index among them, run once per release"),
)  # fmt: skip


def scale(replicas: int) -> None:
    subprocess.run(
        ["kubectl", "--context", "windops", "--namespace", "windops-observability", "scale",
         *OBSERVABILITY, f"--replicas={replicas}"],
        capture_output=True, check=False,
    )  # fmt: skip


def main(only: list[str]) -> int:
    unknown = set(only) - {target for target, _ in CHECKS}
    if unknown:
        print(f"Not a gate check: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    chosen = [(t, proves) for t, proves in CHECKS if not only or t in only]
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-11-gate-"))
    print(f"Logs: {logs}", flush=True)
    failed: list[str] = []
    started = time.monotonic()
    scale(0)
    try:
        for target, proves in chosen:
            # The coverage reads the browser run's report: nothing on the cluster to wait for.
            note = "" if target == "browser-coverage-check" else calm()
            passed, seconds, log = run((target,), logs)
            if passed:
                print(f"ok - {target}{note} ({seconds} s): {proves}", flush=True)
            else:
                print(f"not ok - {target}{note} ({seconds} s): {proves}. See {log}", flush=True)
                failed.append(target)
    finally:
        scale(1)
    minutes = int((time.monotonic() - started) // 60)
    if failed:
        print(f"Phase 11 exit gate failed in {minutes} minutes: {', '.join(failed)}",
              file=sys.stderr)  # fmt: skip
        return 1
    if only:
        print(f"The {len(chosen)} checks named passed in {minutes} minutes")
        return 0
    print(f"Phase 11 exit gate passed in {minutes} minutes: all {len(CHECKS)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
