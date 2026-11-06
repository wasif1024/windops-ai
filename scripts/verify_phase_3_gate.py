#!/usr/bin/env python3
"""Check the Phase 3 exit gate on the real local cluster.

SRS section 12: cross-tenant and missing-permission tests all fail safely, and administrator MFA
is enforced. This runs the route sweep, which calls every Web BFF route as callers who must be
refused, then every Phase 3 check, and the Audit Service's, which Phase 3 changed. Each check's
output goes to its own log. Every check runs, so one failure does not hide another.

Run `make dev` once first, or the setup in infrastructure/minikube/README.md. It takes about 40
minutes, as some checks wait out real certificate renewals, outages and the gateway's sign-in limit.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
# (make target, what it proves for the gate, whether it signs in through the gateway)
CHECKS = (
    (
        "route-sweep",
        "every route refuses a caller without the permission or a second factor",
        False,
    ),
    ("cross-tenant-check", "no API reaches another tenant's data", False),
    ("mfa-check", "administrators must set up and use a second factor", False),
    ("identity-check", "Keycloak's realm, tokens and tenant claim", False),
    ("mtls-check", "services accept only mutual TLS", False),
    ("data-tls-check", "every data store accepts only TLS, through a real renewal", False),
    ("authorization-check", "deny by default, scopes and break-glass", False),
    ("identity-service-check", "user lifecycle, sessions and MFA status", False),
    ("tenant-check", "tenants, branding and row-level security", False),
    ("audit-check", "the audit trail, with its new reading permission", False),
    ("edge-check", "HTTPS at the gateway and its upstream checks", True),
    ("bff-check", "Web BFF with real sign-ins", True),
    ("gateway-policy-check", "token checks, CORS and per-address limits at the edge", False),
    ("token-exchange-check", "short-lived tokens for one service", True),
    ("admin-api-check", "the administrator console's API", True),
    ("platform-api-check", "the Platform console's API", False),
    ("security-check", "the Security dashboard and alerts", False),
    ("frontend-check", "both consoles and the shell in a real browser, phone and desktop", True),
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


def main() -> int:
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-3-gate-"))
    print(f"Logs: {logs}", flush=True)
    failed: list[str] = []
    started = time.monotonic()
    for target, proves, signs_in in CHECKS:
        if signs_in:
            time.sleep(SIGN_IN_WINDOW_SECONDS)
        passed, seconds, log = run(target, logs)
        if passed:
            print(f"ok - {target} ({seconds} s): {proves}", flush=True)
        else:
            failed.append(target)
            print(f"not ok - {target} ({seconds} s): {proves}. See {log}", flush=True)
    minutes = int((time.monotonic() - started) // 60)
    if failed:
        print(
            f"Phase 3 exit gate failed in {minutes} minutes: {', '.join(failed)}", file=sys.stderr
        )
        return 1
    print(f"Phase 3 exit gate passed in {minutes} minutes: all {len(CHECKS)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
