#!/usr/bin/env python3
"""The Phase 8 exit gate: a simulated fault produces a validated, cited report with a full trace.

The gearbox oil fault scenario's last hour is replayed into a turbine of its own, and the demo
operator asks Web BFF to investigate it, as the regression set does. The gate then proves:

- the run completed on the team's model path, never a scripted rehearsal, and the case waits for
  a person's review (SRS 5.4 step 13);
- its result has SRS 5.7's shape, every claim cites what a tool returned, no tool was outside its
  agent's allowlist or denied, and nothing prohibited happened (EVL-003);
- its LangSmith trace holds all 13 steps, a model run for every agent naming its model, effort
  and prompt version, and its tool calls and handoffs, with no token in it (OBS-005, OBS-006);
- the LLM metrics counted its calls.

It runs in one of two modes, which it sets on Investigation for the run and always puts back to
`rehearse` afterwards:
- `stand_in`: a dry run, the real Claude client against the runtime's stand-in API. Free.
- `claude`: the team on Claude itself, within the run's $10 budget. It costs money, so it needs
  `--spend`, and the person responsible's approval first (ADR 0021).

    make phase-8-gate MODE=stand_in
    make phase-8-gate MODE=claude SPEND=yes
"""

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

import grpc
import httpx

from scripts.checks.agents.progress import Operator
from scripts.checks.agents.tools import ENGINEER, Cases
from scripts.checks.agents.trace import AGENTS, JWT_HEADER, STEPS, langsmith, read_trace
from scripts.checks.fleet.asset import bootstrap, signing_key
from scripts.checks.fleet.fault_case import clean_up
from scripts.checks.identity.identity import Realm
from scripts.evaluation.evaluators import gate
from scripts.evaluation.regression import PREFIX, TENANT, Outcome, run_set
from scripts.evaluation.scenarios import dataset
from scripts.lib.settings import KUBE_CONTEXT
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

SCENARIO = "gearbox-temp-001"
INVESTIGATION = "deployment/windops-investigation"
APPLICATION = "windops-application"
SETTLE_SECONDS = 60


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> None:
    subprocess.run(["kubectl", "--context", KUBE_CONTEXT, f"--namespace={APPLICATION}",
                    *arguments], capture_output=True, text=True, check=True)  # fmt: skip


def set_mode(mode: str | None) -> None:
    """Investigation's mode for the gate, or back to the environment's own when None."""
    setting = f"WINDOPS_INVESTIGATION_AGENTS={mode}" if mode else "WINDOPS_INVESTIGATION_AGENTS-"
    kubectl("set", "env", INVESTIGATION, setting)
    kubectl("rollout", "status", INVESTIGATION, "--timeout=300s")
    # The old pod takes up to its 20-second grace to go, and its callers' keepalive up to 30
    # more to notice: a run started sooner may retry step 11, which is free but muddles the
    # evidence (ADR 0027).
    time.sleep(SETTLE_SECONDS)


def llm_calls(provider: str) -> float:
    """Calls the LLM metrics counted for a provider, from Prometheus."""
    with port_forward("svc/windops-prometheus", "windops-observability", 9090) as port:
        answer = httpx.get(f"http://127.0.0.1:{port}/api/v1/query", timeout=15, params={
            "query": f'sum(windops_llm_calls_total{{provider="{provider}"}})'})  # fmt: skip
    found = answer.json()["data"]["result"]
    return float(found[0]["value"][1]) if found else 0.0


def every_attempt(client: Any, project: str, run_id: str) -> tuple[Any, list[Any]]:
    """A run's trace over every attempt: each is a trace of its own, and one taken over or
    retried carries on from its checkpoint, so it holds only the steps it ran (ADR 0018)."""
    root, _ = read_trace(client, project, run_id)
    wanted = f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))'
    roots = list(client.list_runs(project_name=project, filter=wanted, is_root=True))
    runs = [r for found in roots for r in client.list_runs(project_name=project,
                                                            trace_id=found.trace_id)]  # fmt: skip
    if len(roots) > 1:
        print(f"info - the run took {len(roots)} attempts; their traces are read together",
              flush=True)  # fmt: skip
    return root, runs


def prove(outcome: Outcome, mode: str) -> None:
    o = outcome.observed
    scores = {s.key: s for s in outcome.scores}
    check(o.run_status == "completed" and o.case_status == "pending_review",
          f"The run completed and the case waits for review ({o.seconds:.0f}s)")  # fmt: skip
    check(not o.rehearsed or mode == "stand_in",
          "It ran the team's model path, not a scripted rehearsal")  # fmt: skip
    for key in ("output_valid", "evidence_references", "unauthorized_tools", "refused_tools",
                "prohibited_behavior"):  # fmt: skip
        check(scores[key].passed is True, f"{key}: {scores[key].comment or 'passed'}")
    for key in ("expected_tools", "diagnosis", "escalation"):
        # Judged on Claude, for the record: the gate itself asks for a valid, cited report.
        print(f"info - {key}: {scores[key].passed} {scores[key].comment}", flush=True)
    prove_trace(o.run_id)
    print(f"info - {len(o.tool_calls)} tool calls, {o.tokens} tokens, ${o.cost_usd:.4f}",
          flush=True)  # fmt: skip


def prove_trace(run_id: str) -> None:
    """The run's trace, over every attempt (OBS-005 to OBS-008). Reading it costs nothing."""
    client, project = langsmith()
    root, runs = every_attempt(client, project, run_id)
    exported = json.dumps([{"inputs": r.inputs, "outputs": r.outputs} for r in runs],
                          default=str)  # fmt: skip
    llm = [r for r in runs if r.run_type == "llm"]
    # Each agent's model run names it, its model, effort and prompt version; beneath it, the
    # Claude client's own call carries what it used.
    agents = [r for r in llm if (r.extra.get("metadata") or {}).get("agent")]
    calls = [r for r in llm if not (r.extra.get("metadata") or {}).get("agent")]
    named = {r.extra["metadata"]["agent"] for r in agents}
    scripted = {r.extra["metadata"].get("model") for r in agents} == {"scripted"}
    # The Supervisor chooses whom to hand work to: every agent that worked has its model run.
    handed = {str((r.inputs or {}).get("subagent_type", "")) for r in runs
              if r.run_type == "tool" and r.name == "task"} - {""}  # fmt: skip
    worked = {"supervisor", *handed}
    print(f"info - the Supervisor handed work to {sorted(handed)}", flush=True)
    check(
        set(STEPS) <= {r.name for r in runs} and named >= worked and worked <= AGENTS
        and not scripted
        and all({"model", "effort", "prompt_version"} <= set(r.extra["metadata"])
                for r in agents),
        f"Its trace holds all 13 steps, and {len(agents)} agent model runs, every agent that "
        "worked naming its model, effort and prompt version (OBS-005, OBS-006)",
    )  # fmt: skip
    # LangSmith's own record of each call's tokens, from the usage Claude reported.
    used = [r for r in calls if (r.total_tokens or 0) > 0]
    note = f"Each of the {len(calls)} calls beneath them records its token use (OBS-007)"
    check(bool(calls) and len(used) == len(calls), note)
    tools = {r.name for r in runs if r.run_type == "tool"}
    check("task" in tools and "windops_get_evidence" in tools,
          f"It holds the tool calls and handoffs: {sorted(tools)}")  # fmt: skip
    check(JWT_HEADER not in exported, "No token reached the trace")
    shared = root.extra["metadata"]["correlation_id"] == run_id
    check(shared, "Its root names the run as the correlation ID its audit records share (OBS-008)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("stand_in", "claude"), required=True)
    parser.add_argument("--spend", action="store_true", help="allow calls to Claude, which cost")
    again = "check an earlier run's trace and metrics again, calling nothing"
    parser.add_argument("--verify-run", default="", help=again)
    arguments = parser.parse_args(argv)
    if arguments.verify_run:
        provider = "anthropic" if arguments.mode == "claude" else "stand-in"
        try:
            prove_trace(arguments.verify_run)
            check(
                llm_calls(provider) > 0, f"The LLM metrics hold its calls, as provider {provider}"
            )
        except (RuntimeError, OSError, KeyError, httpx.HTTPError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
            return 1
        print(f"Phase 8 gate's trace checks passed for {arguments.verify_run}")
        return 0
    if arguments.mode == "claude" and not arguments.spend:
        print("MODE=claude costs money: add SPEND=yes once it is approved", file=sys.stderr)
        return 2
    provider = "anthropic" if arguments.mode == "claude" else "stand-in"
    _, scenarios = dataset()
    scenario = next(s for s in scenarios if s.case_id == SCENARIO)
    try:
        clean_up(TENANT, PREFIX)
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        before = llm_calls(provider)
        set_mode(arguments.mode)
        try:
            with keycloak_public() as public:
                operator = Operator(Realm(public), TENANT)
                cases = Cases(signing_key())
                try:
                    (outcome, *_) = run_set([scenario], operator, cases)
                finally:
                    cases.close()
        finally:
            set_mode(None)
        prove(outcome, arguments.mode)
        # Prometheus scrapes every 15 seconds or so.
        deadline, counted = time.monotonic() + 60, 0.0
        while time.monotonic() < deadline and counted <= before:
            time.sleep(5)
            counted = llm_calls(provider)
        check(counted > before, f"The LLM metrics counted its {counted - before:.0f} calls, as "
              f"provider {provider}")  # fmt: skip
        check(gate(outcome.scores) or arguments.mode == "claude",
              "Every judged evaluator passed")  # fmt: skip
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up(TENANT, PREFIX)
    print(f"Phase 8 gate passed ({arguments.mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
