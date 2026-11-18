#!/usr/bin/env python3
"""Check the Phase 7 exit gate on the real local cluster.

SRS section 12: results carry citations and provenance, and permissions are enforced before
return. `make citation-check` proves it directly, through the MCP servers an agent will use: every
passage found is cited, from the synthetic set's manifest and holding exactly its hashed text; web
research and forecasts name their sources; and another tenant's user, a user scoped to one wind
farm, and a user with no role each get nothing they may not have.

Around it run every Phase 7 check, and the earlier checks covering what Phase 7 changed: token
exchange, which now issues tool scopes; the Authorization Service's service grants; the audit
trail the new services and servers write to; cross-tenant isolation, with the new tables; the
renamed demo models, in assets and Production Impact's curves; the weather forecast the Weather
MCP serves; and the migration Jobs. Each check's output goes to its own log, and every check runs,
so one failure does not hide another.

Run `make dev` once first, or the setup in infrastructure/minikube/README.md, then
`make knowledge-seed`. The cluster needs to reach OpenAI, Tavily and api.open-meteo.com, with
OPENAI_API_KEY and TAVILY_API_KEY set in .env. It makes a few real web searches.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
# (make target, what it proves for the gate)
CHECKS = (
    (
        "citation-check",
        "results carry citations and provenance, and permissions hold before return",
    ),
    (
        "knowledge-check",
        "uploads screened, documents held for release, and embedded through the adapter",
    ),
    ("retrieval-check", "cited passages of current revisions, filtered by tenant and permission"),
    ("research-check", "web research made generic, recorded, flagged and limited"),
    ("mcp-check", "tool tokens for one server and its tools, checked on every call"),
    ("mcp-servers-check", "five MCP servers, over mutual TLS, limited in Redis and audited"),
    ("token-exchange-check", "short-lived tokens for one service, and only for their owner"),
    ("weather-check", "a real Open-Meteo forecast, with its provenance and freshness"),
    ("authorization-check", "roles, scopes, break-glass, service grants and audited denials"),
    ("audit-check", "a tamper-evident audit record"),
    ("cross-tenant-check", "every data path attacked across tenants, and refused"),
    ("asset-check", "the renamed demo models, and assets read by role and scope"),
    ("impact-check", "estimates from the renamed models' power curves, judged by Policy"),
    ("migrate-check", "migration Jobs run once per release"),
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
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-7-gate-"))
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
        print(f"Phase 7 exit gate failed in {minutes} minutes: {', '.join(failed)}",
              file=sys.stderr)  # fmt: skip
        return 1
    print(f"Phase 7 exit gate passed in {minutes} minutes: all {len(CHECKS)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
