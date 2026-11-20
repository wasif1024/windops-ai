#!/usr/bin/env python3
"""Check the Phase 9 exit gate on the real local cluster.

SRS section 12: an agent can create a draft, and all attempts to submit or reserve as an agent are
denied. `make agent-denial-check` proves it directly: an agent drafts in step 12 of a
fault-triggered investigation, then every layer an agent could reach for a submission or a
reservation refuses it, nothing changes, and every refusal is audited.

Around it run every Phase 9 check, and the earlier checks covering what Phase 9 changed: the
team, whose planner now drafts in step 12; the regression set and the Phase 8 gate's dry run,
over the new graph; live progress; agents' tools, identities and tokens, with the new tool
scopes; the MCP servers; Authorization; isolation across tenants, now attacking every service's
database; the audit trail and JetStream, with the new publishers; and the migration Jobs. Each
check's output goes to its own log, and every check runs, so one failure does not hide another.

It spends nothing: every agent run is rehearsed or a dry run against the stand-in API. Left out,
as they cost money or compute and Phase 9 did not change them: `make workflow-check`, which asks
Claude for one plan; `make claude-check`; `make sandbox-check`, which uses LangSmith compute.

Run `make dev` once first, then the seeds, with WINDOPS_INVESTIGATION_AGENTS=rehearse and
WINDOPS_AGENT_STAND_IN=on. About 90 minutes.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
# (make target and its variables, what it proves for the gate)
CHECKS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("agent-denial-check",),
     "an agent drafts, and every attempt to submit or reserve as an agent is denied and audited"),
    (("maintenance-check",), "work orders validated, drafted by anyone, submitted by a person"),
    (("inventory-check",), "parts, compatibility, stock and reservations, by a person alone"),
    (("planner-check",), "the planner's proposal and one draft in step 12, rehearsed and dry run"),
    (("frontend-check",), "every console journey, the planner screens included, on a phone too"),
    (("team-check",), "the team rehearsed: an operator's planner is refused its draft"),
    (("regression",), "the regression set, rehearsed, over the new graph"),
    (("phase-8-gate", "MODE=stand_in"), "the Phase 8 gate's dry run, through step 12"),
    (("progress-check",), "live progress through the gateway, to pending review"),
    (("agent-tools-check",), "WindOps MCP's tools, with real agent tokens"),
    (("agent-identity-check",), "delegations, agent tokens, agents only read and draft"),
    (("token-exchange-check",), "tool scopes issued only for their owner"),
    (("mcp-servers-check",), "the MCP servers over mutual TLS, limited and audited"),
    (("authorization-check",), "roles, scopes, service grants and audited denials"),
    (("cross-tenant-check",), "every service's database and API attacked across tenants"),
    (("audit-check",), "a tamper-evident audit record"),
    (("nats-check",), "JetStream keeps events and limits each publisher"),
    (("migrate-check",), "migration Jobs run once per release"),
)  # fmt: skip


def run(target: tuple[str, ...], logs: Path) -> tuple[bool, int, Path]:
    log = logs / f"{target[0]}.log"
    started = time.monotonic()
    with log.open("w") as output:
        result = subprocess.run(
            ["make", "--no-print-directory", *target],
            cwd=REPOSITORY, stdout=output, stderr=subprocess.STDOUT, check=False,
        )  # fmt: skip
    return result.returncode == 0, int(time.monotonic() - started), log


def main() -> int:
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-9-gate-"))
    print(f"Logs: {logs}", flush=True)
    failed: list[str] = []
    started = time.monotonic()
    for target, proves in CHECKS:
        passed, seconds, log = run(target, logs)
        name = " ".join(target)
        if passed:
            print(f"ok - {name} ({seconds} s): {proves}", flush=True)
        else:
            print(f"not ok - {name} ({seconds} s): {proves}. See {log}", flush=True)
            failed.append(name)
    minutes = int((time.monotonic() - started) // 60)
    if failed:
        print(f"Phase 9 exit gate failed in {minutes} minutes: {', '.join(failed)}",
              file=sys.stderr)  # fmt: skip
        return 1
    print(f"Phase 9 exit gate passed in {minutes} minutes: all {len(CHECKS)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
