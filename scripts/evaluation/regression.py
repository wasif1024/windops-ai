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

**The Evaluation Service keeps the record and gives the verdict (M12.1).** The runner registers the
dataset version, starts a job in that mode, leases it, and follows the plan the service made,
reporting each attempt as it runs. An attempt that cannot run is reported as such. The service
passes the job only when every planned attempt passed; anything missing leaves it incomplete,
never passed (SRS 7.7). The job is then compared with its benchmark, and `--benchmark` makes a
passed job the benchmark.

With `LANGSMITH_API_KEY` in .env, the dataset is synchronized to LangSmith and the scores are kept
there as an experiment, with the versions behind them (EVL-005). Everything the runner made is
removed at the end.

    make regression                        # every scenario
    make regression SCENARIO=gearbox-temp-001
    make regression BENCHMARK=1            # and make it the benchmark, if it passes
"""

import argparse
import asyncio
import dataclasses
import json
import secrets
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import grpc
import httpx
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops_identity.targets import AGENT_TOOLS
from windops_simulator.documents import EVALUATION_TENANT
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
from scripts.evaluation import judges, pipeline, stored
from scripts.evaluation.evaluators import Observed, Score, ToolCall, evaluate, gate
from scripts.evaluation.pipeline import check_lock
from scripts.evaluation.pipeline import to_langsmith as sync_dataset
from scripts.evaluation.scenarios import Replay, Scenario, dataset
from scripts.evaluation.service import Evaluations, Job, evaluations, mode_now
from scripts.lib import settings
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import authority_file, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import GATEWAY, NAMESPACE, keycloak_public

# The most the LLM judges may spend in one regression, in USD, unless the settings say otherwise.
JUDGE_BUDGET = "5.00"
# The evaluation tenant: its web research and forecasts are recordings (EVL-001, M12.6), its
# library holds the evaluation documents, and a demo tenant never sees either.
TENANT, FARM, PREFIX = EVALUATION_TENANT, "regression-farm", "regr-"
ADMIN = "regression-admin"
ASSET_HOST = "windops-asset.windops-application.svc.cluster.local"
# The scenario's last hour: its fault at full severity, inside a case's default evidence window.
REPLAY = timedelta(minutes=60)
# How long one investigation may take, rehearsed or on Claude.
INVESTIGATION_SECONDS = 1200


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


def deliveries(replayed: Replay, turbine: str, end: datetime) -> list[Any]:
    """The scenario's last hour on this turbine, ending now, as its device would send it, with the
    scenario's own seed: a variant's noise and link trouble are its own (M12.2)."""
    simulator_id, seed = replayed
    base = dataclasses.replace(SCENARIOS[simulator_id], seed=seed)
    start = end - base.duration
    spec = dataclasses.replace(base.turbines[0], tenant_id=TENANT, wind_farm_id=FARM,
                               turbine_id=turbine)  # fmt: skip
    faults = tuple(dataclasses.replace(f, turbine_id=turbine) for f in base.faults)
    scenario = dataclasses.replace(base, start=start, turbines=(spec,), faults=faults)
    sent, _ = run(scenario)
    cutoff = end - REPLAY
    return [d.payload for d in sent if d.sent_at >= cutoff]


def replay(assets: Caller, replayed: Replay) -> str:
    simulator_id, seed = replayed
    base = SCENARIOS[simulator_id]
    turbine = f"{PREFIX}{simulator_id}-{secrets.token_hex(2)}"
    make_turbine(assets, turbine, base.turbines[0])
    hold_detector(turbine)
    items = deliveries(replayed, turbine, datetime.now(UTC).replace(microsecond=0))
    token = device_token(TENANT, turbine)
    context = ssl.create_default_context(cafile=authority_file())
    with port_forward(GATEWAY, NAMESPACE, 443) as port, httpx.Client(verify=context) as client:
        url = f"https://localhost:{port}/ingest/v1/samples"
        for first in range(0, len(items), 600):
            send(client, url, token, items[first : first + 600])
    info(f"replayed {simulator_id}'s last hour, seed {seed}, on {turbine}: {len(items)} deliveries")
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
        case_id=case_id,
    )  # fmt: skip


@dataclasses.dataclass
class Outcome:
    scenario: Scenario
    observed: Observed
    scores: list[Score]


# The evaluation tenant's agents get each scenario's recorded external answer (M12.6). Off, a
# scenario with a recording is skipped on Claude, never judged on the live source (SRS 7.7).
REPLAYS_RECORDINGS = True
SKIPPED = "its recorded external answer is not replayed"


def replays(scenario: Scenario) -> bool:
    """Whether the agents would see this scenario's recording, if it has one."""
    return scenario.recorded_external_sources is None or REPLAYS_RECORDINGS


def judge_client() -> tuple[Any, list[judges.Judge]] | None:
    """Claude as the "evaluator" role, under the judges' own budget, and the locked rubrics.
    None when no key is set: a run on Claude then has no judge, and its judged scores fail."""
    from windops_claude import Budget, Claude, MemoryLedger, Meter
    from windops_claude import load as load_policy

    values = settings.load()
    key = values.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    cap = values.get("WINDOPS_JUDGE_BUDGET_USD", "") or JUDGE_BUDGET
    budget = Budget(tokens=5_000_000, tool_calls=1, cost_usd=Decimal(cap))
    claude = Claude(load_policy(), Meter(budget, MemoryLedger()), lambda: key)
    return claude, judges.load()


def run_set(
    scenarios: Sequence[Scenario],
    operator: Operator,
    cases: Cases,
    job: Job | None = None,
    judging: tuple[Any, list[judges.Judge]] | None = None,
) -> list[Outcome]:
    """Each attempt the job planned, in its order, reported as it finishes.

    With no job, as the Phase 8 gate runs one scenario, each scenario runs its runs_required
    times, or once when rehearsed, and an attempt that cannot run raises."""
    by_id = {s.case_id: s for s in scenarios}
    planned = job.planned() if job else [
        (s.case_id, n) for s in scenarios for n in range(1, s.runs_required + 1)
    ]  # fmt: skip
    rehearsed: set[str] = set()
    key, bundle = signing_key(), client_bundle()
    bootstrap(TENANT, ADMIN, "tenant-administrator")
    turbines: dict[Replay, str] = {}
    with (
        port_forward("deployment/windops-asset", "windops-application", 50051) as port,
        blocking_channel(port, ASSET_HOST, bundle) as channel,
    ):
        assets = Caller(asset_pb2_grpc.AssetServiceStub(channel), key)
        for replayed in dict.fromkeys(by_id[i].replay for i, _ in planned):
            turbines[replayed] = replay(assets, replayed)
    outcomes: list[Outcome] = []
    for scenario_id, attempt in planned:
        scenario = by_id[scenario_id]
        if job is None and scenario_id in rehearsed:
            # A rehearsal's model is scripted, so a repeat would only repeat it (EVL-002).
            continue
        if job is not None:
            job.renew()
            if job.evaluation.mode == "claude" and not replays(scenario):
                # Claude would see the live source, not the recording, so the attempt would
                # judge nothing it is for: skipped, never passed (SRS 7.7).
                job.skip(scenario_id, attempt, SKIPPED)
                info(f"{scenario_id} run {attempt}: skipped: {SKIPPED}")
                continue
            # Its recording, or the baseline, is what the agents' external tools get (EVL-001).
            job.set_replay(scenario_id if scenario.recorded_external_sources else "")
        try:
            case_id = investigate(operator, scenario, turbines[scenario.replay], attempt)
            cases.opened.append(case_id)
            observed = observe(scenario, operator, case_id, follow(operator, case_id))
        except (RuntimeError, httpx.HTTPError, KeyError, ValueError) as error:
            if job is None:
                raise
            # Reported, so the job cannot pass without it (SRS 7.7).
            job.could_not_run(scenario_id, attempt, f"{type(error).__name__}: {error}")
            info(f"{scenario_id} run {attempt}: could not run: {error}")
            continue
        scores = evaluate(scenario, observed, AGENT_TOOLS)
        # The LLM judges, after the deterministic checks and only if they passed (EVL-003,
        # EVL-004), while the run's evidence is still there.
        if judging is not None and not observed.rehearsed:
            judged, cost = asyncio.run(judges.judge_run(
                judging[0], judging[1], scenario, observed, scores,
                stored.evidence_of(TENANT, str((observed.result or {}).get("snapshot_id", ""))),
                pipeline.ground_truth(scenario),
            ))  # fmt: skip
            scores = [*scores, *judged]
            observed.cost_usd += float(cost)
        outcomes.append(Outcome(scenario, observed, scores))
        if observed.rehearsed:
            rehearsed.add(scenario_id)
        status = ("passed" if gate(scores) else "failed") if job is None else (
            job.report(scenario_id, attempt, observed, scores, versions()).status
        )  # fmt: skip
        info(f"{scenario_id} run {attempt} of {len(planned)}: {status} in "
             f"{observed.seconds:.0f}s, {len(observed.tool_calls)} tool calls, "
             f"${observed.cost_usd:.4f}")  # fmt: skip
    return outcomes


def verdict(api: Evaluations, job: Job, benchmark: bool) -> bool:
    """The service's outcome, and the job against its benchmark. True when it passed."""
    done = job.finish()
    print(f"\nEvaluation {done.job_id} ({done.mode}, {done.dataset_name} "
          f"{done.dataset_version}): {done.status}, {done.passed} of {done.planned} attempts "
          f"passed, {done.failed} failed, {done.incomplete} incomplete")  # fmt: skip
    for a in done.attempts:
        if a.status not in ("passed", "planned"):
            print(f"    {a.scenario_id} run {a.attempt}: {a.status}: {a.reason}")
    compared = api.compare(done.job_id)
    if compared.benchmark.job_id:
        print(f"Against benchmark {compared.benchmark.job_id}: {compared.regressed} regressed, "
              f"{compared.fixed} fixed, cost {compared.cost_usd_change:+.4f} USD")  # fmt: skip
        for c in compared.scenarios:
            if c.change in ("regressed", "fixed"):
                print(f"    {c.scenario_id}: {c.change} ({c.benchmark_passed}/"
                      f"{c.benchmark_planned} then, {c.passed}/{c.planned} now)")  # fmt: skip
    else:
        print("No benchmark yet for this dataset, splits and mode")
    if benchmark and done.status == "passed":
        made = api.benchmark(done.job_id)
        print(f"Made {made.job_id} the benchmark for {made.dataset_name} ({made.mode})")
    return bool(done.status == "passed")


def gated(api: Evaluations, job_id: str, scenarios: Sequence[Scenario]) -> bool:
    """The job's report, written, and its SRS 10.7 gates (M12.8). True when it passed."""
    from windops.evaluation.v1 import evaluation_pb2 as pb

    from scripts.evaluation import report as reports

    done = api.call("GetEvaluation", pb.GetEvaluationRequest(job_id=job_id)).evaluation
    found = reports.build(done, scenarios)
    path = reports.write(found)
    for each in found.gates:
        print(f"{each.status:>10} - {each.name}: {each.detail}")
    print(f"Report {found.outcome}: {path}")
    return found.outcome == "passed"


def report(outcomes: Sequence[Outcome]) -> None:
    """Print each run's scores. The Evaluation Service gives the verdict."""
    keys = [s.key for s in outcomes[0].scores] if outcomes else []
    print("\n" + " | ".join(["scenario", *keys]))
    for o in outcomes:
        marks = ["pass" if s.passed else "-" if s.passed is None else "FAIL" for s in o.scores]
        print(" | ".join([o.scenario.case_id, *marks]))
        for score in o.scores:
            if score.passed is False:
                print(f"    {score.key}: {score.comment}")
    failed = [o.scenario.case_id for o in outcomes if not gate(o.scores)]
    print(f"\n{len(outcomes) - len(failed)} of {len(outcomes)} runs scored passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))  # fmt: skip


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
            "policy_hash": pinned["policy_hash"],
            # The rubrics each judged score was given by (EVL-004, EVL-005).
            **judges.versions(judges.load())}  # fmt: skip


def to_langsmith(header: dict[str, str], scenarios: Sequence[Scenario],
                 outcomes: Sequence[Outcome]) -> None:  # fmt: skip
    """Keep the dataset in LangSmith, and the scores as an experiment on it (EVL-005)."""
    from langsmith import evaluate

    synced = sync_dataset(header, scenarios)
    if synced is None:
        return
    client, found = synced
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
    parser.add_argument("--benchmark", action="store_true",
                        help="make the job the benchmark, if it passes")  # fmt: skip
    parser.add_argument("--split", choices=("train", "validation", "held_out"),
                        help="run only this split, as a job of that split")  # fmt: skip
    parser.add_argument("--report", action="store_true",
                        help="write the job's report; pass only if its gates pass")  # fmt: skip
    arguments = parser.parse_args(argv)
    header, scenarios = dataset()
    try:
        # A version is run only as locked, so its results name what they scored (EVL-005).
        check_lock()
    except ValueError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    chosen = scenarios
    if arguments.scenario:
        unknown = set(arguments.scenario) - {s.case_id for s in scenarios}
        if unknown:
            print(f"No such scenarios: {sorted(unknown)}", file=sys.stderr)
            return 2
        chosen = [s for s in scenarios if s.case_id in arguments.scenario]
    if arguments.split:
        chosen = [s for s in chosen if s.split == arguments.split]
    passed = False
    try:
        clean_up(TENANT, PREFIX, FARM)
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        with evaluations() as api:
            api.register(header, scenarios)
            mode = mode_now()
            # A whole split is a job of that split, so its benchmark and report are the split's.
            only = [s.case_id for s in chosen] if arguments.scenario else []
            if not only and not arguments.split:
                only = [s.case_id for s in chosen]
            job = api.start(header, mode, only, [arguments.split] if arguments.split else [])
            info(f"evaluation {job.job_id}, {mode}: {job.evaluation.planned} attempts planned")
            try:
                with keycloak_public() as public:
                    operator = Operator(Realm(public), TENANT)
                    cases = Cases(signing_key(), TENANT)
                    try:
                        # The LLM judges only judge a model's own work (EVL-004).
                        judging = judge_client() if mode == "claude" else None
                        outcomes = run_set(scenarios, operator, cases, job, judging)
                    finally:
                        cases.close()
            finally:
                # Whatever happened, the service decides: what did not run is not passed.
                passed = verdict(api, job, arguments.benchmark)
                if arguments.report:
                    passed = gated(api, job.job_id, scenarios) and passed
        report(outcomes)
        if not arguments.no_langsmith:
            to_langsmith(header, scenarios, outcomes)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up(TENANT, PREFIX, FARM)
    print("Regression passed" if passed else "Regression failed")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
