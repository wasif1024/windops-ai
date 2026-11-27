#!/usr/bin/env python3
"""Prove the Evaluation Service on the cluster (M12.1, SRS 7.2): datasets, jobs, benchmarks.

With the real service, its PostgreSQL and the real Authorization Service, it registers a dataset
of its own, of three check scenarios, and runs jobs on it without running any investigation: it
reports each attempt itself, as `make regression` does. It proves:

- only a Platform Administrator manages evaluation: a Tenant Administrator and an agent acting
  for the Platform Administrator are refused (SRS 3.2, SIM-005);
- a dataset version never changes: registering it again is a no-op, with other content refused,
  and the database itself refuses a change (EVL-005);
- a rehearsed job plans one attempt a scenario, and one on Claude each scenario's runs, five for a
  security case (EVL-002);
- a job passes only when every planned attempt passed. One failure fails it. A skipped attempt, a
  rehearsed one in a job on Claude, or one never reported leaves it incomplete, never passed
  (SRS 7.7);
- only the lease holder reports, and once its lease runs out another runner takes the job over,
  keeping what was reported; a reported attempt is kept as it was, and the database refuses a
  change;
- a passed job becomes the benchmark, and a later job compared with it names what regressed;
- the jobs' starts and outcomes reach the audit trail;
- the recorded providers (M12.6, EVL-001) answer only a caller with a certificate from the
  cluster's CA, give a scenario's recording only while its job's runner has set it and the lease
  holds, and the baseline otherwise; and only the lease holder sets it.

It spends nothing. Run `make services` first. What it makes is removed at the end.
"""

import json
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import httpx
from windops.evaluation.v1 import evaluation_pb2 as pb
from windops.evaluation.v1 import evaluation_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.maintenance import sql as service_sql
from scripts.checks.identity.agent_identity import as_user, code_of
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import PLATFORM_TENANT

HOST = "windops-evaluation.windops-application.svc.cluster.local"
RUN = str(int(time.time()))
DATASET = f"evaluation-check-{RUN}"
ADMIN, TENANT_ADMIN = "evaluation-check-platform", "evaluation-check-tenant-admin"
TENANT = "north-sea"
REFUSED = (grpc.StatusCode.FAILED_PRECONDITION,)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def sql(statement: str) -> str:
    return service_sql(statement, "windops_evaluation")


def refused_by_database(statement: str) -> bool:
    try:
        sql(statement)
    except subprocess.CalledProcessError:
        return True
    return False


# The check's weather scenario's recorded forecast carries this note (M12.6).
NOTE = f"Evaluation check note {RUN}"


def definition(scenario_id: str, title: str) -> str:
    written: dict[str, Any] = {"case_id": scenario_id, "ground_truth": "gearbox_oil_overheating",
                               "case": {"title": title}}  # fmt: skip
    if scenario_id == "check-weather-001":
        written["recording"] = {"tool": "weather_get_forecast", "source": "open-meteo",
                                "text": NOTE}  # fmt: skip
    return json.dumps(written)


def scenarios(title: str = "Gearbox running hot") -> list[pb.Scenario]:
    return [
        pb.Scenario(scenario_id="check-gearbox-001", family="gearbox_diagnostics",
                    split="held_out", severity="high", runs_required=1,
                    definition_json=definition("check-gearbox-001", title)),
        pb.Scenario(scenario_id="check-injection-001", family="rbac_and_security",
                    split="held_out", severity="high", runs_required=5,
                    definition_json=definition("check-injection-001", "Gearbox alarm")),
        pb.Scenario(scenario_id="check-weather-001", family="weather_impact", split="train",
                    severity="medium", runs_required=1,
                    definition_json=definition("check-weather-001", "Wind is rising")),
    ]  # fmt: skip


def scores(passed: bool = True) -> list[pb.Score]:
    return [
        pb.Score(key="output_valid", result="pass" if passed else "fail",
                 comment="" if passed else "No hypotheses"),
        pb.Score(key="diagnosis", result="not_judged", comment="A rehearsal"),
    ]  # fmt: skip


class Service:
    def __init__(self, stub: Any, key: str) -> None:
        self.caller = Caller(stub, key)
        self.stub, self.key = stub, key

    def call(self, method: str, request: Any, who: str = ADMIN,
             tenant: str = PLATFORM_TENANT) -> Any:  # fmt: skip
        return self.caller.call(method, request, who, tenant)

    def code(self, method: str, request: Any, **who: Any) -> grpc.StatusCode:
        return code_of(lambda: getattr(self.stub, method)(request, **who))

    def start(self, mode: str, only: list[str] | None = None) -> Any:
        return self.call("StartEvaluation", pb.StartEvaluationRequest(
            dataset_name=DATASET, dataset_version="1", mode=mode, scenario_ids=only or [],
        )).evaluation  # fmt: skip

    def lease(self, job_id: str, runner: str = "evaluation-check-a", seconds: int = 600) -> Any:
        return self.call("LeaseEvaluation", pb.LeaseEvaluationRequest(
            job_id=job_id, runner=runner, lease_seconds=seconds))  # fmt: skip

    def report(self, job_id: str, token: str, scenario_id: str, attempt: int = 1,
               **values: Any) -> Any:  # fmt: skip
        values.setdefault("scores", scores())
        return self.call("ReportAttempt", pb.ReportAttemptRequest(
            job_id=job_id, lease_token=token,
            attempt=pb.Attempt(scenario_id=scenario_id, attempt=attempt,
                               run_id=f"run-check-{RUN}-{scenario_id}-{attempt}", **values),
            versions={"check": RUN},
        )).attempt  # fmt: skip

    def finish(self, job_id: str, token: str) -> Any:
        return self.call("FinishEvaluation", pb.FinishEvaluationRequest(
            job_id=job_id, lease_token=token)).evaluation  # fmt: skip

    def run(self, mode: str, failing: Callable[[str, int], bool] | None = None,
            only: list[str] | None = None) -> Any:  # fmt: skip
        job = self.start(mode, only)
        token = self.lease(job.job_id).lease_token
        for a in job.attempts:
            bad = bool(failing and failing(a.scenario_id, a.attempt))
            self.report(job.job_id, token, a.scenario_id, a.attempt, scores=scores(not bad))
        return self.finish(job.job_id, token)


def check_access(service: Service) -> None:
    listing = pb.ListDatasetsRequest()
    check(service.code("ListDatasets", listing, **as_user(ADMIN, service.key, PLATFORM_TENANT))
          == grpc.StatusCode.OK, "A Platform Administrator manages evaluation")  # fmt: skip
    check(
        service.code("ListDatasets", listing, **as_user(TENANT_ADMIN, service.key, TENANT))
        == grpc.StatusCode.PERMISSION_DENIED,
        "A Tenant Administrator may not: evaluation.manage is the Platform's alone (SRS 3.2)",
    )
    agent = as_user(ADMIN, service.key, PLATFORM_TENANT, actor="agent:fault-investigator")
    check(service.code("ListDatasets", listing, **agent) == grpc.StatusCode.PERMISSION_DENIED,
          "No agent may, even acting for a Platform Administrator: the ground truth is never an "
          "agent's (SIM-005)")  # fmt: skip


def check_datasets(service: Service) -> None:
    register = pb.RegisterDatasetRequest(
        name=DATASET,
        version="1",
        scenarios=scenarios(),
        description="The Evaluation Service check's own",
    )
    first = service.call("RegisterDataset", register)
    check(
        first.created and first.dataset.scenario_count == 3,
        f"{DATASET} version 1 is registered, three scenarios, ground truth kept here",
    )
    again = service.call("RegisterDataset", register)
    check(not again.created and again.dataset.content_sha256 == first.dataset.content_sha256,
          "Registering the same version again changes nothing")  # fmt: skip
    changed = pb.RegisterDatasetRequest(
        name=DATASET, version="1", scenarios=scenarios("Gearbox oil running hot")
    )
    check(service.code("RegisterDataset", changed, **as_user(ADMIN, service.key, PLATFORM_TENANT))
          in REFUSED, "The same version with other content is refused (EVL-005)")  # fmt: skip
    check(refused_by_database(
        f"UPDATE dataset_scenarios SET split = 'train' WHERE dataset_name = '{DATASET}'"  # noqa: S608
    ), "The database itself refuses to change a registered dataset")  # fmt: skip


def check_jobs(service: Service) -> Any:
    rehearsed = service.start("rehearse")
    check(rehearsed.planned == 3 and rehearsed.status == "queued",
          "A rehearsed job plans one attempt a scenario")  # fmt: skip
    service.call(
        "CancelEvaluation",
        pb.CancelEvaluationRequest(job_id=rehearsed.job_id, reason="Planned only"),
    )
    on_claude = service.start("claude", ["check-injection-001"])
    check(on_claude.planned == 5, "On Claude, a security case runs 5 times (EVL-002)")
    leased = service.lease(on_claude.job_id)
    for a in on_claude.attempts:
        service.report(on_claude.job_id, leased.lease_token, a.scenario_id, a.attempt,
                       scores=scores(a.attempt != 3))  # fmt: skip
    failed = service.finish(on_claude.job_id, leased.lease_token)
    check((failed.status, failed.passed, failed.failed) == ("failed", 4, 1),
          "One failure in five fails the job (EVL-002)")  # fmt: skip

    skipped = service.start("claude", ["check-gearbox-001", "check-weather-001"])
    token = service.lease(skipped.job_id).lease_token
    kept = service.report(skipped.job_id, token, "check-gearbox-001", rehearsed=True)
    check(kept.status == "skipped",
          "A rehearsed attempt in a job on Claude is recorded as skipped")  # fmt: skip
    ended = service.finish(skipped.job_id, token)
    check(
        (ended.status, ended.passed, ended.incomplete) == ("incomplete", 0, 2),
        "A skipped or unreported attempt leaves the job incomplete, never passed (SRS 7.7)",
    )

    taken = service.start("rehearse", ["check-gearbox-001", "check-weather-001"])
    first = service.lease(taken.job_id, "evaluation-check-a", seconds=2)
    service.report(taken.job_id, first.lease_token, "check-gearbox-001")
    wrong = pb.ReportAttemptRequest(
        job_id=taken.job_id,
        lease_token="not-this-one",  # noqa: S106  # Not a secret: a wrong token.
        attempt=pb.Attempt(scenario_id="check-weather-001", attempt=1, scores=scores()),
    )
    check(service.code("ReportAttempt", wrong, **as_user(ADMIN, service.key, PLATFORM_TENANT))
          in REFUSED, "Only the runner holding the lease reports")  # fmt: skip
    time.sleep(3)
    # Named, so a job some other runner left behind is never the one taken over here.
    second = service.lease(taken.job_id, "evaluation-check-b")
    picked = {a.scenario_id: a.status for a in second.evaluation.attempts}
    check(second.evaluation.job_id == taken.job_id and picked["check-gearbox-001"] == "passed",
          "Once its lease runs out, another runner takes the job over, keeping what was "
          "reported")  # fmt: skip
    check(refused_by_database(
        "UPDATE evaluation_attempts SET status = 'failed' "  # noqa: S608
        f"WHERE job_id = '{taken.job_id}' AND scenario_id = 'check-gearbox-001'"
    ), "The database itself refuses to change a reported attempt")  # fmt: skip
    service.report(taken.job_id, second.lease_token, "check-weather-001")
    service.finish(taken.job_id, second.lease_token)
    return failed


def check_benchmarks(service: Service) -> None:
    base = service.run("rehearse")
    check(base.status == "passed" and base.passed == 3,
          "A job passes when every planned attempt passed")  # fmt: skip
    made = service.call("SetBenchmark", pb.SetBenchmarkRequest(job_id=base.job_id)).benchmark
    check(made.job_id == base.job_id, "A passed job becomes the benchmark")
    later = service.run("rehearse", failing=lambda scenario_id, _: scenario_id ==
                        "check-weather-001")  # fmt: skip
    compared = service.call("CompareEvaluation", pb.CompareEvaluationRequest(job_id=later.job_id))
    regressed = [c.scenario_id for c in compared.scenarios if c.change == "regressed"]
    check(compared.benchmark.job_id == base.job_id and regressed == ["check-weather-001"],
          "Compared with its benchmark, a later job names what regressed")  # fmt: skip


def forecast(port: int, bundle: ClientBundle | None) -> dict[str, Any]:
    """The recorded forecast, as the Weather Service asks for it, with or without a certificate."""
    with tempfile.TemporaryDirectory() as folder:
        context = ssl.create_default_context()
        authority = Path(folder) / "ca.crt"
        authority.write_bytes(bundle.authority if bundle else client_bundle().authority)
        context.load_verify_locations(str(authority))
        if bundle is not None:
            (Path(folder) / "tls.crt").write_bytes(bundle.certificate)
            (Path(folder) / "tls.key").write_bytes(bundle.key)
            context.load_cert_chain(str(Path(folder) / "tls.crt"), str(Path(folder) / "tls.key"))
        with httpx.Client(verify=context, timeout=15) as http:
            answer = http.get(f"https://localhost:{port}/replay/open-meteo/v1/forecast",
                              params={"latitude": "54.8", "longitude": "2.1"},
                              extensions={"sni_hostname": HOST})  # fmt: skip
            answer.raise_for_status()
            found: dict[str, Any] = answer.json()
            return found


def check_replay(service: Service, bundle: ClientBundle) -> None:
    job = service.start("rehearse")
    token = service.lease(job.job_id).lease_token
    with port_forward("deployment/windops-evaluation", "windops-application", 8443) as port:
        try:
            forecast(port, None)
            refused = False
        except (httpx.HTTPError, ssl.SSLError):
            refused = True
        check(refused, "The recordings refuse a caller with no certificate from the cluster's CA")
        check("notes" not in forecast(port, bundle) and len(forecast(port, bundle)["hourly"]
                                                              ["time"]) == 24 * 8,
              "With nothing set, the recordings give the baseline: a fresh forecast")  # fmt: skip
        wrong = pb.SetReplayRequest(job_id=job.job_id, lease_token=f"{token}-not-it",
                                    scenario_id="check-weather-001")  # fmt: skip
        check(service.code("SetReplay", wrong, **as_user(ADMIN, service.key, PLATFORM_TENANT))
              in REFUSED, "Only the lease holder sets what is replayed")  # fmt: skip
        missing = pb.SetReplayRequest(job_id=job.job_id, lease_token=token,
                                      scenario_id="no-such-scenario")  # fmt: skip
        unknown = service.code("SetReplay", missing,
                               **as_user(ADMIN, service.key, PLATFORM_TENANT))  # fmt: skip
        check(
            unknown == grpc.StatusCode.NOT_FOUND, "Only a scenario of the job's dataset is replayed"
        )
        set_ = service.call("SetReplay", pb.SetReplayRequest(
            job_id=job.job_id, lease_token=token, scenario_id="check-weather-001"))  # fmt: skip
        check(
            set_.tool == "weather_get_forecast" and forecast(port, bundle).get("notes") == NOTE,
            "A scenario's recording is what the recorded provider gives while it is set",
        )
        service.call("SetReplay", pb.SetReplayRequest(job_id=job.job_id, lease_token=token))
        check("notes" not in forecast(port, bundle), "Cleared, the baseline is given again")
        service.call("SetReplay", pb.SetReplayRequest(
            job_id=job.job_id, lease_token=token, scenario_id="check-weather-001"))  # fmt: skip
        for a in job.attempts:
            service.report(job.job_id, token, a.scenario_id, a.attempt)
        service.finish(job.job_id, token)
        check(
            "notes" not in forecast(port, bundle),
            "Once its job ends, a recording is never given again",
        )


def audited(since: datetime) -> set[str]:
    statement = (
        "SELECT set_config('windops.all_tenants', 'on', false); "  # noqa: S608  # Our clock.
        "SELECT DISTINCT action FROM audit_records WHERE action LIKE 'evaluation.%' "
        f"AND recorded_at >= '{since.isoformat()}'"
    )
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-data", "exec",
         "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
         "--no-align", "--username=windops_audit", "--dbname=windops_audit",
         "--set=ON_ERROR_STOP=1", f"--command={statement}"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return {line.strip() for line in result.stdout.splitlines() if line.startswith("evaluation.")}


def clean_up() -> None:
    """This run's own dataset, jobs, attempts and benchmark, its triggers paused to do it."""
    jobs = f"SELECT job_id FROM evaluation_jobs WHERE dataset_name = '{DATASET}'"  # noqa: S608
    sql("; ".join([
        "ALTER TABLE evaluation_attempts DISABLE TRIGGER attempts_reported_once",
        "ALTER TABLE datasets DISABLE TRIGGER datasets_never_change",
        "ALTER TABLE dataset_scenarios DISABLE TRIGGER dataset_scenarios_never_change",
        f"DELETE FROM benchmarks WHERE dataset_name = '{DATASET}'",  # noqa: S608
        f"DELETE FROM evaluation_attempts WHERE job_id IN ({jobs})",  # noqa: S608
        f"DELETE FROM evaluation_jobs WHERE dataset_name = '{DATASET}'",  # noqa: S608
        f"DELETE FROM dataset_scenarios WHERE dataset_name = '{DATASET}'",  # noqa: S608
        f"DELETE FROM datasets WHERE name = '{DATASET}'",  # noqa: S608
        "ALTER TABLE evaluation_attempts ENABLE TRIGGER attempts_reported_once",
        "ALTER TABLE datasets ENABLE TRIGGER datasets_never_change",
        "ALTER TABLE dataset_scenarios ENABLE TRIGGER dataset_scenarios_never_change",
        "SELECT 1",
    ]))  # fmt: skip


def main() -> int:
    started = datetime.now(UTC)
    try:
        bootstrap(PLATFORM_TENANT, ADMIN, "platform-administrator")
        bootstrap(TENANT, TENANT_ADMIN, "tenant-administrator")
        bundle = client_bundle()
        with (
            port_forward("deployment/windops-evaluation", "windops-application", 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            service = Service(evaluation_pb2_grpc.EvaluationServiceStub(channel), signing_key())
            try:
                check_access(service)
                check_datasets(service)
                check_jobs(service)
                check_benchmarks(service)
                check_replay(service, bundle)
                # The outbox relay sends them; give it a moment.
                deadline, expected = time.monotonic() + 60, {
                    "evaluation.dataset.registered", "evaluation.started", "evaluation.passed",
                    "evaluation.failed", "evaluation.incomplete", "evaluation.benchmark.set",
                }  # fmt: skip
                while not expected <= audited(started) and time.monotonic() < deadline:
                    time.sleep(3)
                check(
                    expected <= audited(started),
                    "Datasets, starts, outcomes and benchmarks reach the audit trail",
                )
            finally:
                clean_up()
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Evaluation check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
