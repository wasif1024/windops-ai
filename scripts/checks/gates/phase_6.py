#!/usr/bin/env python3
"""Check the Phase 6 exit gate on the real local cluster.

SRS section 12: boundary tests pass 100%, and a missing weather input fails closed. Two checks
prove it directly:
- `make boundary-check` runs every boundary test, generated from the default libraries, and
  needs 100%;
- `make weather-outage-check` takes the weather away each way it can go (the provider down, only
  a stale forecast kept, the Weather Service down) and requires every climb to block, then puts
  it back.

Around them run every Phase 6 check, and the earlier checks covering what Phase 6 changed: the
Authorization Service's service grants, the audit trail the new services write to, cross-tenant
isolation, and the migration Jobs, as windops-migrations' dependencies changed. The weather outage
check runs last, as it takes the Weather Service down. Each check's output goes to its own log,
and every check runs, so one failure does not hide another.

Run `make dev` once first, or the setup in infrastructure/minikube/README.md. The cluster needs
to reach api.open-meteo.com. It takes about 15 minutes.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
# (make target, what it proves for the gate)
CHECKS = (
    ("edge-check", "the gateway serves only HTTPS, and only Keycloak's sign-in pages"),
    ("boundary-check", "every boundary test, generated from the default libraries, passes 100%"),
    ("policy-check", "proposals judged on gathered facts and the real forecast, recorded for ever"),
    (
        "impact-check",
        "estimates with ranges from live telemetry and the forecast, judged by Policy",
    ),
    ("weather-check", "a real Open-Meteo forecast, with its provenance and freshness"),
    ("authorization-check", "roles, scopes, break-glass, service grants and audited denials"),
    ("audit-check", "a tamper-evident audit record"),
    ("cross-tenant-check", "every data path attacked across tenants, and refused"),
    ("migrate-check", "migration Jobs run once per release, with windops-migrations' dependencies"),
    ("weather-outage-check", "a missing weather input fails closed, each way it can go missing"),
)


def run(target: str, logs: Path) -> tuple[bool, int, Path]:
    log = logs / f"{target}.log"
    started = time.monotonic()
    with log.open("w") as output:
        result = subprocess.run(
            ["make", "--no-print-directory", target],
            cwd=REPOSITORY, stdout=output, stderr=subprocess.STDOUT, check=False,
        )  # fmt: skip
    return result.returncode == 0, int(time.monotonic() - started), log


def main() -> int:
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-6-gate-"))
    print(f"Logs: {logs}", flush=True)
    failed: list[str] = []
    started = time.monotonic()
    for target, proves in CHECKS:
        passed, seconds, log = run(target, logs)
        if passed:
            print(f"ok - {target} ({seconds} s): {proves}", flush=True)
        else:
            print(f"not ok - {target} ({seconds} s): {proves}. See {log}", flush=True)
            failed.append(target)
    minutes = int((time.monotonic() - started) // 60)
    if failed:
        print(f"Phase 6 exit gate failed in {minutes} minutes: {', '.join(failed)}",
              file=sys.stderr)  # fmt: skip
        return 1
    print(f"Phase 6 exit gate passed in {minutes} minutes: all {len(CHECKS)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
