#!/usr/bin/env python3
"""Prove step 12 on the cluster: the Maintenance Planner's proposal and draft work order (M9.3).

A case is opened on the demo turbine and its evidence frozen. As for a fault-triggered
investigation, the Investigation Service delegates to agents for the tenant's fault-investigator,
the principal that may draft (SRS 9.2). Two investigation runs follow, at no cost:

- a rehearsal: the real team, tools and tokens, each agent's model a script;
- a dry run: the real Claude client, against the runtime's stand-in API (ADR 0027).

For each, the check proves:

- the run completed, and in step 5 the planner advised without drafting;
- in step 12 it read the turbine's history, found parts, and drafted exactly one work order for
  the recommended action, through WindOps MCP with its own token;
- the work order is in the Maintenance Service as a draft by agent:maintenance-planner for the
  fault-investigator, validated, and never submitted;
- the run's result keeps the work order, the action and the planner's proposal;
- the draft was audited as the agent, on the principal's behalf (SEC-010);
- the rehearsal called no model, and the dry run's planner went through the Claude client to the
  stand-in API, never to Anthropic.

Run `make services` first, with WINDOPS_AGENT_STAND_IN=on. The case and its drafts are removed.
"""

import json
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime

import grpc
import httpx
from windops.agent_runtime.v1 import agent_runtime_pb2 as rt
from windops.identity.v1 import identity_pb2

from scripts.checks.agents.runtime import INVESTIGATION, investigation_bundle, runtime, wait_for
from scripts.checks.agents.team import final_state, run_sql
from scripts.checks.agents.tools import TURBINE, Cases
from scripts.checks.fleet.asset import bootstrap, signing_key
from scripts.checks.fleet.maintenance import clean_up as clean_up_work_orders
from scripts.checks.fleet.maintenance import sql as maintenance_sql
from scripts.checks.identity.agent_identity import as_service, audited, identity_as
from scripts.checks.identity.token_exchange import TENANT

RUN = str(int(time.time()))
OPERATOR = "planner-check-operator"
AGENT, PRINCIPAL = "agent:maintenance-planner", "service:fault-investigator"
DRAFT = "windops_create_draft_work_order"
MODES = {
    "rehearsal": {"team": "true", "rehearse": "true", "turbine_id": TURBINE},
    "dry run": {"team": "true", "stand_in": "true"},
}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def delegate(key: str, case_id: str, mode: str) -> str:
    """For the fault-investigator, as Investigation does for a fault-triggered case."""
    with identity_as(investigation_bundle()) as identity:
        return str(
            identity.StartDelegation(
                identity_pb2.StartDelegationRequest(
                    case_id=case_id, idempotency_key=f"planner-{RUN}-{mode.replace(' ', '-')}"
                ),
                **as_service("service:investigation", key, tenant=TENANT),
            ).delegation.delegation_id
        )


def investigate(key: str, case_id: str, snapshot: str, mode: str) -> str:
    with runtime(investigation_bundle(), key) as api:
        run = api.call(
            "StartRun",
            rt.StartRunRequest(
                case_id=case_id,
                snapshot_id=snapshot,
                workflow="investigation",
                parameters=MODES[mode],
                requested_by=PRINCIPAL,
                idempotency_key=f"planner-{RUN}-{mode.replace(' ', '-')}",
                delegation_id=delegate(key, case_id, mode),
            ),
            INVESTIGATION,
            TENANT,
            level=None,
        ).run
    ended = wait_for(
        f"the {mode} to finish",
        lambda: (s := run_sql("SELECT status || '|' || reason FROM runs WHERE run_id = :run",
                              run.run_id)).split("|")[0] not in ("queued", "running") and s,
        timeout=900,
    )  # fmt: skip
    status, _, reason = str(ended).partition("|")
    if status != "completed":
        raise RuntimeError(f"The {mode} ended as {status}: {reason}")
    return str(run.run_id)


def prove(run_id: str, case_id: str, mode: str) -> str:
    state = final_state(run_id)
    used = list(state["tools_used"])
    drafts = [t for t in used if t == DRAFT]
    step_12 = used[-3:]
    check(
        step_12 == ["windops_list_cases", "windops_find_parts", DRAFT] and len(drafts) == 1,
        f"{mode}: the planner advised in step 5 without drafting, then in step 12 read the "
        "history, found parts, and drafted once",
    )
    kept = json.loads(run_sql("SELECT result_json FROM runs WHERE run_id = :run", run_id))
    order = kept.get("work_order") or {}
    work_order_id = str(order.get("work_order_id", ""))
    check(
        work_order_id.startswith("wo_")
        and order.get("drafted_by_agent") == AGENT
        and kept["proposed_action"] == kept["recommended_actions"][0]["action"]
        and bool(kept["proposal"]),
        f"{mode}: the result keeps {work_order_id}, its {kept['proposed_action']}, and the "
        "planner's proposal",
    )
    if not work_order_id.isascii() or not work_order_id[3:].isalnum():
        raise RuntimeError(f"Not a work order's ID: {work_order_id!r}")
    row = maintenance_sql(
        "SELECT status || '|' || drafted_by || '|' || drafted_by_agent || '|' || case_id || '|' "  # noqa: S608  # A checked ID.
        f"|| submitted_by FROM work_orders WHERE work_order_id = '{work_order_id}'"
    )
    check(
        row == f"draft|{PRINCIPAL}|{AGENT}|{case_id}|",
        f"{mode}: the Maintenance Service holds it as a draft by the planner agent for the "
        "fault-investigator, never submitted",
    )
    return work_order_id


def main() -> int:
    try:
        key = signing_key()
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        started = datetime.now(UTC)
        with ExitStack() as stack:
            cases = Cases(key)
            stack.callback(cases.close)
            stack.callback(lambda: clean_up_work_orders(cases.opened))
            case_id = cases.open(OPERATOR, title=f"Planner check {RUN}")
            snapshot = cases.evidence(case_id)
            drafted = []
            for mode in MODES:
                run_id = investigate(key, case_id, snapshot, mode)
                drafted.append(prove(run_id, case_id, mode))
                calls = run_sql("SELECT count(*) FILTER (WHERE role = 'maintenance-planner') "
                                "|| '|' || count(*) FROM run_calls WHERE run_id = :run",
                                run_id)  # fmt: skip
                planned, _, total = calls.partition("|")
                if mode == "rehearsal":
                    check(total == "0", "rehearsal: no model was called, so it cost nothing")
                else:
                    check(int(planned) >= 4,
                          f"dry run: the planner's {planned} model calls, in steps 5 and 12, went "
                          "through the Claude client to the stand-in API, at no cost")  # fmt: skip
            count = maintenance_sql(
                f"SELECT count(*) FROM work_orders WHERE case_id = '{case_id}'"  # noqa: S608  # Its own case.
            )
            check(count == "2" and len(set(drafted)) == 2, "One draft for each run, and no other")
        deadline, seen = time.monotonic() + 60, set()
        while time.monotonic() < deadline:
            seen = {
                (action, behalf)
                for action, kind, actor, behalf in audited(started)
                if kind == "agent" and actor == AGENT
            }
            if ("mcp.windops.create_draft_work_order", PRINCIPAL) in seen:
                break
            time.sleep(3)
        check(
            {("mcp.windops.find_parts", PRINCIPAL),
             ("mcp.windops.create_draft_work_order", PRINCIPAL)} <= seen,
            "Its parts search and draft were audited as the agent, on the fault-investigator's "
            "behalf (SEC-010)",
        )  # fmt: skip
    except (RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Planner check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
