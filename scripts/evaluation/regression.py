#!/usr/bin/env python3
"""Run the regression set on the cluster, and score each run deterministically (M8.12, SRS 10.6).

For each simulator scenario the set uses, a turbine of its own is made in north-sea, and the
scenario's last hour is replayed through the gateway's device route, time-shifted to end now, link
trouble and all. The detector is held off it, so no detection opens a case of its own. Then, for
each scenario on that turbine, the demo operator asks Web BFF to investigate it, as a person would
(API-003), and the stream is followed until it says the investigation is finished.

What each run did is read back:
- **The run:** its status, result, cost and duration, from the Agent Runtime.
- **Its tool calls:** every MCP tool and Sandbox task call, from the audit trail, by its
  correlation ID.

The deterministic evaluators score each run (EVL-003). Runs are made the way Investigation's
`WINDOPS_INVESTIGATION_AGENTS` says:
- **Rehearsed** (the default until M8.13): once each, at no cost, and only what the platform
  decides is judged.
- **On Claude:** each scenario's `runs_required` times (EVL-002), with diagnosis, escalation and
  expected tools judged too.

With `LANGSMITH_API_KEY` in .env, the dataset is synchronized to LangSmith and the scores are kept
there as an experiment, with the versions behind them (EVL-005). Everything the runner made is
removed at the end.

    make regression                        # every scenario
    make regression SCENARIO=gearbox-temp-001
"""

import argparse
import dataclasses
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import certifi
import grpc
import httpx
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops_identity.targets import AGENT_TOOLS
from windops_simulator.scenarios import SCENARIOS, run

from scripts.checks.agents.progress import API, Operator, events
from scripts.checks.agents.runtime import sql as runtime_sql
from scripts.checks.agents.tools import ENGINEER, Cases
from scripts.checks.fleet import fault_rules
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.data_quality import device_token
from scripts.checks.fleet.fault_case import clean_up
from scripts.checks.fleet.fault_stream import send
from scripts.checks.identity.identity import Realm
from scripts.evaluation.evaluators import Observed, Score, ToolCall, evaluate, gate
from scripts.evaluation.scenarios import Scenario, dataset
from scripts.lib import settings
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import authority_file, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import GATEWAY, NAMESPACE, keycloak_public

TENANT, FARM, PREFIX = "north-sea", "regression-farm", "regr-"
ADMIN = "regression-admin"
ASSET_HOST = "windops-asset.windops-application.svc.cluster.local"
# The scenario's last hour: its fault at full severity, inside a case's default evidence window.
REPLAY = timedelta(minutes=60)
# How long one investigation may take, rehearsed or on Claude.
INVESTIGATION_SECONDS = 1200
DATASET_NAME = "windops-first-regression"


def info(message: str) -> None:
    print(f"info - {message}", flush=True)


def make_turbine(assets: Caller, turbine: str, spec: Any) -> None:
    try:
        assets.call("CreateWindFarm", asset_pb2.CreateWindFarmRequest(
            wind_farm_id=FARM, name="Regression replays", country="GB",
            time_zone="Europe/London", location=asset_pb2.Location(latitude=54.0, longitude=1.0),
            offshore=True,
        ), ADMIN, TENANT)  # fmt: skip
    except grpc.RpcError as error:
        if error.code() != grpc.StatusCode.ALREADY_EXISTS:
            raise
    assets.call("CreateTurbine", asset_pb2.CreateTurbineRequest(
        turbine_id=turbine, wind_farm_id=FARM, name=f"Replay {turbine}",
        location=asset_pb2.Location(latitude=54.0, longitude=1.0),
        model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic", model="WS-8.0-167",
                                     rated_power_kw=spec.rated_power_kw,
                                     rotor_diameter_m=spec.rotor_diameter_m),
    ), ADMIN, TENANT)  # fmt: skip


def hold_detector(turbine: str) -> None:
    """Keep the detector off the replay for a day, so it opens no case of its own."""
    fault_rules.last_line(fault_rules.sql(
        "INSERT INTO turbine_watch (tenant_id, turbine_id, last_seen_at, last_evaluated_at) "  # noqa: S608  # Constants and our own turbine ID.
        f"VALUES ('{TENANT}', '{turbine}', now(), now() + interval '1 day') "
        "ON CONFLICT (tenant_id, turbine_id) DO UPDATE SET "
        "last_evaluated_at = now() + interval '1 day'; SELECT 1", tenant=TENANT,
    ))  # fmt: skip


def deliveries(simulator_id: str, turbine: str, end: datetime) -> list[Any]:
    """The scenario's last hour on this turbine, ending now, as its device would send it."""
    base = SCENARIOS[simulator_id]
    start = end - base.duration
    spec = dataclasses.replace(base.turbines[0], tenant_id=TENANT, wind_farm_id=FARM,
                               turbine_id=turbine)  # fmt: skip
    faults = tuple(dataclasses.replace(f, turbine_id=turbine) for f in base.faults)
    scenario = dataclasses.replace(base, start=start, turbines=(spec,), faults=faults)
    sent, _ = run(scenario)
    cutoff = end - REPLAY
    return [d.payload for d in sent if d.sent_at >= cutoff]


def replay(assets: Caller, simulator_id: str) -> str:
    base = SCENARIOS[simulator_id]
    turbine = f"{PREFIX}{simulator_id}-{secrets.token_hex(2)}"
    make_turbine(assets, turbine, base.turbines[0])
    hold_detector(turbine)
    items = deliveries(simulator_id, turbine, datetime.now(UTC).replace(microsecond=0))
    token = device_token(TENANT, turbine)
    context = ssl.create_default_context(cafile=authority_file())
    with port_forward(GATEWAY, NAMESPACE, 443) as port, httpx.Client(verify=context) as client:
        url = f"https://localhost:{port}/ingest/v1/samples"
        for first in range(0, len(items), 600):
            send(client, url, token, items[first : first + 600])
    info(f"replayed {simulator_id}'s last hour on {turbine}: {len(items)} deliveries")
    return turbine


def investigate(operator: Operator, scenario: Scenario, turbine: str, attempt: int) -> str:
    key = f"regression-{scenario.case_id}-{attempt}-{secrets.token_hex(4)}"
    body = {"turbine_id": turbine, "title": scenario.case.title,
            "severity": scenario.case.severity}  # fmt: skip
    # Investigation may have just restarted, as the gate does to set its mode: an unavailable
    # answer is retried with the same key, which can only ever make one case.
    deadline = time.monotonic() + 90
    while True:
        answer = httpx.post(f"{API}/investigations", json=body, timeout=30,
                            headers=operator.headers(**{"Idempotency-Key": key}))  # fmt: skip
        if answer.status_code != 503 or time.monotonic() > deadline:
            break
        time.sleep(5)
    if answer.status_code != 202:
        raise RuntimeError(f"Investigating answered {answer.status_code}: {answer.text[:300]}")
    return str(answer.json()["case_id"])


def follow(operator: Operator, case_id: str) -> str:
    """The run's ID, once the stream says the investigation is finished."""
    run_id, after = "", 0
    deadline = time.monotonic() + INVESTIGATION_SECONDS
    while time.monotonic() < deadline:
        try:
            for event in events(operator, case_id, after):
                if event["event"] == "finished":
                    return run_id
                after = int(event["id"])
                run_id = event["data"]["run_id"] or run_id
        except httpx.HTTPError:
            time.sleep(3)
    raise RuntimeError(f"{case_id}'s investigation did not finish in time")


def _json(statement: str) -> Any:
    lines = runtime_sql(statement, tenant=TENANT).strip().splitlines()
    return json.loads(lines[-1]) if lines else None


def tool_calls(run_id: str) -> list[ToolCall]:
    """Every MCP tool and Sandbox task the run's agents called, from the audit trail."""
    statement = (
        "SELECT set_config('windops.all_tenants', 'on', false); SELECT "  # noqa: S608  # The runtime's own run ID.
        "coalesce(json_agg(json_build_object('actor', actor_id, 'action', action, "
        "'outcome', outcome)), '[]') FROM audit_records WHERE correlation_id = "
        f"'{run_id}' AND (action LIKE 'mcp.%' OR action LIKE 'agent_run.sandbox.%')"
    )
    out = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-data", "exec",
         "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
         "--no-align", "--username=windops_audit", "--dbname=windops_audit",
         "--set=ON_ERROR_STOP=1", f"--command={statement}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]  # fmt: skip
    return [
        # "mcp.<server>.<tool>", or "agent_run.sandbox.<task>" for a Sandbox task.
        ToolCall(agent=r["actor"].removeprefix("agent:"),
                 scope=r["action"].removeprefix("agent_run."), outcome=r["outcome"])
        for r in json.loads(out)
    ]  # fmt: skip


def observe(scenario: Scenario, operator: Operator, case_id: str, run_id: str) -> Observed:
    case = httpx.get(f"{API}/investigations/{case_id}", headers=operator.headers(),
                     timeout=15).json()  # fmt: skip
    found = _json(
        "SELECT row_to_json(r) FROM (SELECT status, reason, parameters, result_json, "  # noqa: S608  # The runtime's own run ID.
        "extract(epoch FROM finished_at - created_at) AS seconds FROM runs "
        f"WHERE run_id = '{run_id}') r"
    )
    used = _json(
        "SELECT json_build_object('tokens', coalesce(sum(processed_tokens), 0), "  # noqa: S608
        f"'cost', coalesce(sum(cost_usd), 0)) FROM run_calls WHERE run_id = '{run_id}'"
    )
    if not found:
        raise RuntimeError(f"No run {run_id} for {scenario.case_id}")
    parameters = json.loads(found["parameters"])
    # The audit trail is written through the outbox: give it a moment to arrive.
    time.sleep(5)
    return Observed(
        scenario_id=scenario.case_id, run_id=run_id, run_status=found["status"],
        case_status=case["status"],
        # A rehearsal and a dry run are both scripted: nothing a model chose is judged.
        rehearsed=bool(parameters.get("rehearse") or parameters.get("stand_in")),
        result=json.loads(found["result_json"]) if found["result_json"] else None,
        tool_calls=tool_calls(run_id), seconds=float(found["seconds"] or 0),
        tokens=int(used["tokens"]), cost_usd=float(used["cost"]), reason=found["reason"],
    )  # fmt: skip


@dataclasses.dataclass
class Outcome:
    scenario: Scenario
    observed: Observed
    scores: list[Score]


def run_set(scenarios: Sequence[Scenario], operator: Operator, cases: Cases) -> list[Outcome]:
    key, bundle = signing_key(), client_bundle()
    bootstrap(TENANT, ADMIN, "tenant-administrator")
    turbines: dict[str, str] = {}
    with (
        port_forward("deployment/windops-asset", "windops-application", 50051) as port,
        blocking_channel(port, ASSET_HOST, bundle) as channel,
    ):
        assets = Caller(asset_pb2_grpc.AssetServiceStub(channel), key)
        for simulator_id in dict.fromkeys(s.simulator_scenario for s in scenarios):
            turbines[simulator_id] = replay(assets, simulator_id)
    outcomes: list[Outcome] = []
    for scenario in scenarios:
        attempt, runs = 0, 1
        while attempt < runs:
            attempt += 1
            case_id = investigate(operator, scenario, turbines[scenario.simulator_scenario],
                                  attempt)  # fmt: skip
            cases.opened.append(case_id)
            observed = observe(scenario, operator, case_id, follow(operator, case_id))
            # A rehearsal's model is scripted, so a repeat would only repeat it (EVL-002).
            runs = 1 if observed.rehearsed else scenario.runs_required
            scores = evaluate(scenario, observed, AGENT_TOOLS)
            outcomes.append(Outcome(scenario, observed, scores))
            info(f"{scenario.case_id} run {attempt} of {runs}: "
                 f"{'passed' if gate(scores) else 'FAILED'} in {observed.seconds:.0f}s, "
                 f"{len(observed.tool_calls)} tool calls, ${observed.cost_usd:.4f}")  # fmt: skip
    return outcomes


def report(outcomes: Sequence[Outcome]) -> bool:
    """Print each run's scores. True when every run passed."""
    keys = [s.key for s in outcomes[0].scores] if outcomes else []
    print("\n" + " | ".join(["scenario", *keys]))
    for o in outcomes:
        marks = ["pass" if s.passed else "-" if s.passed is None else "FAIL" for s in o.scores]
        print(" | ".join([o.scenario.case_id, *marks]))
        for score in o.scores:
            if score.passed is False:
                print(f"    {score.key}: {score.comment}")
    failed = [o.scenario.case_id for o in outcomes if not gate(o.scores)]
    print(f"\n{len(outcomes) - len(failed)} of {len(outcomes)} runs passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))  # fmt: skip
    return not failed


def versions() -> dict[str, Any]:
    """What the runs depend on (EVL-005): the graph, prompts, tools and skills, and the policy."""
    pinned = _json(
        "SELECT row_to_json(r) FROM (SELECT pins, policy_version, policy_hash FROM runs "
        "WHERE workflow = 'investigation' ORDER BY created_at DESC LIMIT 1) r"
    )
    if not pinned:
        return {}
    pins = json.loads(pinned["pins"])
    return {"graph": pins.get("graph"), "prompts": pins.get("prompts"), "tools": pins.get("tools"),
            "skills": pins.get("skills"), "policy_version": pinned["policy_version"],
            "policy_hash": pinned["policy_hash"]}  # fmt: skip


def to_langsmith(header: dict[str, str], scenarios: Sequence[Scenario],
                 outcomes: Sequence[Outcome]) -> None:  # fmt: skip
    """Keep the dataset in LangSmith, and the scores as an experiment on it (EVL-005)."""
    from langsmith import Client, evaluate

    values = settings.load()
    if not values.get("LANGSMITH_API_KEY"):
        info("no LANGSMITH_API_KEY: the dataset and scores stay local")
        return
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    client = Client(api_key=values["LANGSMITH_API_KEY"])
    if client.has_dataset(dataset_name=DATASET_NAME):
        found = client.read_dataset(dataset_name=DATASET_NAME)
    else:
        found = client.create_dataset(DATASET_NAME, description=header["description"],
                                      metadata={"version": header["version"]})  # fmt: skip
    existing = {str(e.metadata.get("scenario_id")): e
                for e in client.list_examples(dataset_id=found.id) if e.metadata}  # fmt: skip
    for s in scenarios:
        inputs = {"title": s.case.title, "severity": s.case.severity, "replay": s.input_fixture}
        # The ground truth stays out of the inputs: the agents never see it (SIM-005).
        outputs = {
            "ground_truth": s.ground_truth,
            "expected_behavior": s.expected_behavior,
            "prohibited_behavior": s.prohibited_behavior,
            "expected_outcome": s.expected_outcome,
            "expect": s.expect.model_dump(),
        }
        metadata = {"scenario_id": s.case_id, "family": s.family,
                    "dataset_version": s.dataset_version}  # fmt: skip
        if s.case_id in existing:
            client.update_example(existing[s.case_id].id, inputs=inputs, outputs=outputs,
                                  metadata=metadata, split=s.split)  # fmt: skip
        else:
            client.create_examples(dataset_id=found.id, examples=[
                {"inputs": inputs, "outputs": outputs, "metadata": metadata, "split": s.split}
            ])  # fmt: skip
    info(f"synchronized {len(scenarios)} scenarios to LangSmith's {DATASET_NAME}")
    if not outcomes:
        return
    by_id: dict[str, list[Outcome]] = {}
    for o in outcomes:
        by_id.setdefault(o.scenario.case_id, []).append(o)
    by_example = {str(e.metadata.get("scenario_id")): e
                  for e in client.list_examples(dataset_id=found.id) if e.metadata}  # fmt: skip
    # One example per run, so a scenario run 5 times on Claude has 5 (EVL-002).
    examples = [by_example[o.scenario.case_id] for o in outcomes]
    served: dict[str, Iterator[Outcome]] = {k: iter(v) for k, v in by_id.items()}
    ran: dict[str, Outcome] = {}

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        """Each run's summary, already made on the cluster: nothing runs here."""
        title = inputs["title"]
        scenario_id = next(s.case_id for s in scenarios if s.case.title == title)
        outcome = next(served[scenario_id])
        ran[outcome.observed.run_id] = outcome
        o = outcome.observed
        top = sorted((o.result or {}).get("hypotheses", []),
                     key=lambda h: -float(h.get("confidence", 0)))  # fmt: skip
        return {"run_id": o.run_id, "run_status": o.run_status, "case_status": o.case_status,
                "rehearsed": o.rehearsed, "tools": sorted({c.name for c in o.tool_calls}),
                "escalation_required": bool(o.result and o.result.get("escalation_required")),
                "top_hypothesis": top[0]["statement"][:500] if top else "",
                "seconds": o.seconds, "tokens": o.tokens, "cost_usd": o.cost_usd}  # fmt: skip

    def scored(outputs: dict[str, Any]) -> list[dict[str, Any]]:
        outcome = ran[outputs["run_id"]]
        return [{"key": s.key, "score": None if s.passed is None else int(s.passed),
                 "comment": s.comment} for s in outcome.scores]  # fmt: skip

    def deterministic(outputs: dict[str, Any]) -> dict[str, Any]:
        return {"results": scored(outputs)}

    rehearsed = all(o.observed.rehearsed for o in outcomes)
    evaluate(
        target, data=examples, evaluators=[deterministic],
        experiment_prefix=f"regression-{'rehearsed' if rehearsed else 'claude'}",
        metadata={"dataset_version": header["version"], "rehearsed": rehearsed, **versions()},
        max_concurrency=0, client=client,
    )  # fmt: skip
    info("kept the scores in LangSmith as an experiment")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", action="append", default=[],
                        help="run only this scenario; may be given more than once")  # fmt: skip
    parser.add_argument("--no-langsmith", action="store_true", help="keep everything local")
    arguments = parser.parse_args(argv)
    header, scenarios = dataset()
    if arguments.scenario:
        unknown = set(arguments.scenario) - {s.case_id for s in scenarios}
        if unknown:
            print(f"No such scenarios: {sorted(unknown)}", file=sys.stderr)
            return 2
        scenarios = [s for s in scenarios if s.case_id in arguments.scenario]
    passed = False
    try:
        clean_up(TENANT, PREFIX)
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        with keycloak_public() as public:
            operator = Operator(Realm(public), TENANT)
            cases = Cases(signing_key())
            try:
                outcomes = run_set(scenarios, operator, cases)
            finally:
                cases.close()
        passed = report(outcomes)
        if not arguments.no_langsmith:
            to_langsmith(header, scenarios, outcomes)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up(TENANT, PREFIX)
    print("Regression passed" if passed else "Regression failed")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
