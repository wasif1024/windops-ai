"""The runner's side of the Evaluation Service (M12.1): register, start, lease, report, finish.

The runner holds no verdict of its own. It registers the dataset version it loaded, starts a job,
leases it and follows the job's plan, reporting each attempt as it runs. Finishing the job asks the
service for the outcome: passed only when every planned attempt was reported and passed, so a run
that stopped part way, or a skipped attempt, never passes (SRS 7.7).

It calls the service over mutual TLS as a Platform Administrator of its own, the only role that
holds evaluation.manage (SRS 3.2).
"""

import secrets
import socket
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from windops.evaluation.v1 import evaluation_pb2 as pb
from windops.evaluation.v1 import evaluation_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.evaluation.evaluators import Observed, Score
from scripts.evaluation.pipeline import to_message
from scripts.evaluation.scenarios import Scenario
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import PLATFORM_TENANT

HOST = "windops-evaluation.windops-application.svc.cluster.local"
RUNNER = "evaluation-runner"
# Long enough for a scenario on Claude; renewed before each attempt.
LEASE_SECONDS = 1800


def mode_now() -> str:
    """How Investigation starts runs now, read from its running container, whether the deployment
    or the environment's settings set it: "claude" when on Claude, else "rehearse". A dry run's
    stand-in is scripted too, so it is rehearsed. Unset, Investigation rehearses."""
    found = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-application", "exec",
         "deployment/windops-investigation", "--", "printenv", "WINDOPS_INVESTIGATION_AGENTS"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    # printenv answers 1 when the variable is unset; anything else means it could not be read.
    if found.returncode not in (0, 1):
        raise RuntimeError(f"Investigation's mode cannot be read: {found.stderr.strip()}")
    return "claude" if found.stdout.strip() == "claude" else "rehearse"


def scores_of(scores: Sequence[Score]) -> list[pb.Score]:
    result = {True: "pass", False: "fail", None: "not_judged"}
    return [pb.Score(key=s.key, result=result[s.passed], comment=s.comment[:500]) for s in scores]


@dataclass
class Job:
    """A leased job: what to run, in order, and how to report it."""

    api: "Evaluations"
    evaluation: pb.Evaluation
    token: str

    @property
    def job_id(self) -> str:
        return str(self.evaluation.job_id)

    def planned(self) -> list[tuple[str, int]]:
        return [(a.scenario_id, a.attempt) for a in self.evaluation.attempts
                if a.status == "planned"]  # fmt: skip

    def renew(self) -> None:
        self.api.call("RenewLease", pb.RenewLeaseRequest(
            job_id=self.job_id, lease_token=self.token, lease_seconds=LEASE_SECONDS))  # fmt: skip

    def report(self, scenario_id: str, attempt: int, observed: Observed, scores: Sequence[Score],
               versions: Mapping[str, Any]) -> pb.Attempt:  # fmt: skip
        answer = self.api.call("ReportAttempt", pb.ReportAttemptRequest(
            job_id=self.job_id, lease_token=self.token,
            attempt=pb.Attempt(
                scenario_id=scenario_id, attempt=attempt, run_id=observed.run_id,
                case_id=observed.case_id, rehearsed=observed.rehearsed,
                scores=scores_of(scores), seconds=observed.seconds, tokens=observed.tokens,
                cost_usd=observed.cost_usd,
            ),
            versions={k: str(v) for k, v in versions.items() if v is not None},
        ))  # fmt: skip
        return answer.attempt  # type: ignore[no-any-return]

    def could_not_run(self, scenario_id: str, attempt: int, reason: str) -> pb.Attempt:
        answer = self.api.call("ReportAttempt", pb.ReportAttemptRequest(
            job_id=self.job_id, lease_token=self.token,
            attempt=pb.Attempt(scenario_id=scenario_id, attempt=attempt, status="error",
                               reason=reason[:900]),
        ))  # fmt: skip
        return answer.attempt  # type: ignore[no-any-return]

    def set_replay(self, scenario_id: str) -> str:
        """The scenario whose recorded answer the evaluation tenant's agents get; "" for the
        baseline. Returns the tool it is for, if any."""
        answer = self.api.call("SetReplay", pb.SetReplayRequest(
            job_id=self.job_id, lease_token=self.token, scenario_id=scenario_id))  # fmt: skip
        return str(answer.tool)

    def skip(self, scenario_id: str, attempt: int, reason: str) -> pb.Attempt:
        """Reported as skipped, so the job cannot pass without it (SRS 7.7)."""
        answer = self.api.call("ReportAttempt", pb.ReportAttemptRequest(
            job_id=self.job_id, lease_token=self.token,
            attempt=pb.Attempt(scenario_id=scenario_id, attempt=attempt, status="skipped",
                               reason=reason[:900]),
        ))  # fmt: skip
        return answer.attempt  # type: ignore[no-any-return]

    def finish(self) -> pb.Evaluation:
        return self.api.call("FinishEvaluation", pb.FinishEvaluationRequest(  # type: ignore[no-any-return]
            job_id=self.job_id, lease_token=self.token)).evaluation  # fmt: skip


class Evaluations:
    def __init__(self, caller: Caller) -> None:
        self._caller = caller

    def call(self, method: str, request: Any) -> Any:
        return self._caller.call(method, request, RUNNER, PLATFORM_TENANT)

    def register(self, header: Mapping[str, str], scenarios: Sequence[Scenario]) -> pb.Dataset:
        answer = self.call("RegisterDataset", pb.RegisterDatasetRequest(
            name=header["dataset"], version=header["version"],
            description=header.get("description", "")[:1000],
            scenarios=[to_message(s) for s in scenarios],
        ))  # fmt: skip
        return answer.dataset  # type: ignore[no-any-return]

    def start(self, header: Mapping[str, str], mode: str, only: Sequence[str]) -> Job:
        started = self.call("StartEvaluation", pb.StartEvaluationRequest(
            dataset_name=header["dataset"], dataset_version=header["version"], mode=mode,
            scenario_ids=list(only), note="make regression",
            idempotency_key=f"regression-{secrets.token_hex(8)}",
        )).evaluation  # fmt: skip
        leased = self.call("LeaseEvaluation", pb.LeaseEvaluationRequest(
            job_id=started.job_id, runner=f"make-regression@{socket.gethostname()}"[:200],
            lease_seconds=LEASE_SECONDS,
        ))  # fmt: skip
        return Job(self, leased.evaluation, leased.lease_token)

    def compare(self, job_id: str) -> pb.CompareEvaluationResponse:
        return self.call("CompareEvaluation", pb.CompareEvaluationRequest(job_id=job_id))  # type: ignore[no-any-return]

    def benchmark(self, job_id: str) -> pb.Benchmark:
        return self.call("SetBenchmark", pb.SetBenchmarkRequest(  # type: ignore[no-any-return]
            job_id=job_id, idempotency_key=f"benchmark-{job_id}")).benchmark  # fmt: skip


@contextmanager
def evaluations() -> Iterator[Evaluations]:
    """The Evaluation Service, as the runner's own Platform Administrator."""
    bootstrap(PLATFORM_TENANT, RUNNER, "platform-administrator")
    with (
        port_forward("deployment/windops-evaluation", "windops-application", 50051) as port,
        blocking_channel(port, HOST, client_bundle()) as channel,
    ):
        yield Evaluations(Caller(evaluation_pb2_grpc.EvaluationServiceStub(channel),
                                 signing_key()))  # fmt: skip
