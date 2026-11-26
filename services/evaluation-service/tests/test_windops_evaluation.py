"""Evaluation through a real gRPC server and SQL database, with a stand-in Authorization Service.
SQLite has no row-level security or triggers, so `make evaluation-check` proves those on
PostgreSQL."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.evaluation.v1 import evaluation_pb2 as pb
from windops.evaluation.v1 import evaluation_pb2_grpc
from windops_evaluation import rules
from windops_evaluation.service import EvaluationService
from windops_evaluation.store import METADATA, SERVICE_NAME
from windops_events import parse_event
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, sign_identity
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
PLATFORM, TENANT = "windops-platform", "north-sea"
SETTINGS = Settings(environment="test", version="test", identity_secret=SECRET, grpc_port=0,
                    http_port=0, shutdown_grace_seconds=1)  # fmt: skip
START = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
ADMIN, OTHER_ADMIN, TENANT_ADMIN = "u-platform", "u-platform-2", "u-tenant-admin"
GRANTS = {ADMIN, OTHER_ADMIN}
DATASET, VERSION = "first-regression", "1.0"


def as_caller(who: str = ADMIN, tenant: str = PLATFORM, actor: str | None = None) -> Any:
    identity = VerifiedIdentity(subject=who, tenant_id=tenant, user_id=who,
                                auth_time=int(time.time()), actor=actor)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


def definition(scenario_id: str, **more: Any) -> str:
    return json.dumps({"case_id": scenario_id, "ground_truth": "gearbox_oil_overheating", **more})


def scenario(scenario_id: str, family: str = "gearbox_diagnostics", split: str = "held_out",
             runs: int = 1, severity: str = "high") -> pb.Scenario:  # fmt: skip
    return pb.Scenario(scenario_id=scenario_id, family=family, split=split, severity=severity,
                       runs_required=runs, definition_json=definition(scenario_id))  # fmt: skip


SCENARIOS = [
    scenario("gearbox-temp-001"),
    scenario("injection-001", family="rbac_and_security", runs=5),
    scenario("weather-001", family="weather_impact", split="train"),
]


def passing(*keys: str) -> list[pb.Score]:
    return [pb.Score(key=k, result="pass") for k in keys or ("output_validity",)]


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


@dataclass
class FakeAuthorizer:
    """evaluation.manage for the Platform Administrators only; an agent is never granted it."""

    grants: set[str] = field(default_factory=lambda: set(GRANTS))

    async def allowed(self, permission: str) -> bool:
        from windops_grpc import current_identity

        identity = current_identity()
        return permission == "evaluation.manage" and str(identity.user_id) in self.grants


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    clock: list[datetime]

    async def register(self, scenarios: list[pb.Scenario] | None = None, version: str = VERSION,
                       who: str = ADMIN, key: str = "") -> pb.RegisterDatasetResponse:  # fmt: skip
        answer: pb.RegisterDatasetResponse = await self.stub.RegisterDataset(
            pb.RegisterDatasetRequest(name=DATASET, version=version, description="The first",
                                      scenarios=scenarios or SCENARIOS, idempotency_key=key),
            metadata=as_caller(who), timeout=10,
        )  # fmt: skip
        return answer

    async def start(self, mode: str = "rehearse", splits: tuple[str, ...] = (),
                    only: tuple[str, ...] = (), key: str = "") -> pb.Evaluation:  # fmt: skip
        answer = await self.stub.StartEvaluation(
            pb.StartEvaluationRequest(dataset_name=DATASET, dataset_version=VERSION,
                                      splits=list(splits), scenario_ids=list(only), mode=mode,
                                      idempotency_key=key),
            metadata=as_caller(), timeout=10,
        )  # fmt: skip
        return answer.evaluation  # type: ignore[no-any-return]

    async def lease(self, job_id: str = "", runner: str = "runner-a",
                    seconds: int = 0) -> pb.LeaseEvaluationResponse:  # fmt: skip
        answer: pb.LeaseEvaluationResponse = await self.stub.LeaseEvaluation(
            pb.LeaseEvaluationRequest(runner=runner, job_id=job_id, lease_seconds=seconds),
            metadata=as_caller(), timeout=10,
        )  # fmt: skip
        return answer

    async def report(self, job_id: str, token: str, scenario_id: str, attempt: int = 1,
                     **values: Any) -> pb.Attempt:  # fmt: skip
        values.setdefault("scores", passing())
        answer = await self.stub.ReportAttempt(
            pb.ReportAttemptRequest(job_id=job_id, lease_token=token, attempt=pb.Attempt(
                scenario_id=scenario_id, attempt=attempt, run_id=f"run-{scenario_id}-{attempt}",
                **values,
            ), versions={"graph": "investigation@3"}),
            metadata=as_caller(), timeout=10,
        )  # fmt: skip
        return answer.attempt  # type: ignore[no-any-return]

    async def finish(self, job_id: str, token: str) -> pb.Evaluation:
        answer = await self.stub.FinishEvaluation(
            pb.FinishEvaluationRequest(job_id=job_id, lease_token=token),
            metadata=as_caller(), timeout=10,
        )  # fmt: skip
        return answer.evaluation  # type: ignore[no-any-return]

    async def run_all(self, job: pb.Evaluation, token: str,
                      failing: set[str] | None = None) -> pb.Evaluation:  # fmt: skip
        for a in job.attempts:
            bad = a.scenario_id in (failing or set())
            scores = [pb.Score(key="output_validity", result="fail" if bad else "pass",
                               comment="Missing hypotheses" if bad else "")]  # fmt: skip
            await self.report(job.job_id, token, a.scenario_id, a.attempt, scores=scores,
                              seconds=10, tokens=100, cost_usd=0.5)  # fmt: skip
        return await self.finish(job.job_id, token)

    async def audited(self) -> list[str]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        payloads: list[Any] = [parse_event(r.subject, r.envelope).payload for r in rows]
        return [str(p.action) for p in payloads]


def with_service(scenario_: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    clock = [START]

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)

        def register(server: grpc.aio.Server) -> None:
            evaluation_pb2_grpc.add_EvaluationServiceServicer_to_server(
                EvaluationService(engine, FakeAuthorizer(), clock=lambda: clock[0]), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = evaluation_pb2_grpc.EvaluationServiceStub(channel)
                await scenario_(World(stub, engine, clock))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# --- the rules, alone


def spec(scenario_id: str, family: str = "gearbox_diagnostics", split: str = "held_out",
         runs: int = 1) -> rules.ScenarioSpec:  # fmt: skip
    return rules.ScenarioSpec(scenario_id, family, split, "high", runs, definition(scenario_id))


def test_a_dataset_is_refused_when_it_breaks_srs_10_5() -> None:
    cases = {
        "not one of SRS 10.5's families": [spec("a-001", family="poetry")],
        "the split must be one of": [spec("a-001", split="test")],
        "runs at least 5 times": [spec("a-001", family="safety_escalation", runs=4)],
        "appears twice": [spec("a-001"), spec("a-001")],
        "needs at least one scenario": [],
    }
    for message, scenarios in cases.items():
        with pytest.raises(rules.RuleError, match=message):
            rules.check_dataset("first-regression", "1.0", scenarios)
    wrong = rules.ScenarioSpec("a-001", "gearbox_diagnostics", "train", "high", 1,
                               definition("b-001"))  # fmt: skip
    with pytest.raises(rules.RuleError, match="its own case_id"):
        rules.check_dataset("first-regression", "1.0", [wrong])


def test_the_content_hash_sees_any_change_but_not_key_order() -> None:
    first = rules.content_hash([spec("a-001"), spec("b-001")])
    assert first == rules.content_hash([spec("b-001"), spec("a-001")])
    reordered = rules.ScenarioSpec("a-001", "gearbox_diagnostics", "held_out", "high", 1,
                                   json.dumps({"ground_truth": "gearbox_oil_overheating",
                                               "case_id": "a-001"}))  # fmt: skip
    assert first == rules.content_hash([reordered, spec("b-001")])
    assert first != rules.content_hash([spec("a-001", split="train"), spec("b-001")])


def test_a_plan_runs_each_scenario_once_rehearsed_and_as_asked_on_claude() -> None:
    scenarios = [spec("a-001"), spec("b-001", family="safety_escalation", runs=5)]
    assert rules.plan("rehearse", scenarios, [], []) == [("a-001", 1), ("b-001", 1)]
    assert rules.plan("claude", scenarios, [], []) == [
        ("a-001", 1), *[("b-001", n) for n in range(1, 6)],
    ]  # fmt: skip
    with pytest.raises(rules.RuleError, match="No scenario"):
        rules.plan("rehearse", scenarios, ["train"], [])
    with pytest.raises(rules.RuleError, match="Not in this dataset"):
        rules.plan("rehearse", scenarios, [], ["c-001"])


def test_an_outcome_passes_only_when_every_planned_attempt_passed() -> None:
    """SRS 7.7: a skipped evaluation is never counted as passed."""
    assert rules.outcome(["passed", "passed"]) == "passed"
    assert rules.outcome(["passed", "failed"]) == "failed"
    for unfinished in ("planned", "skipped", "error"):
        assert rules.outcome(["passed", unfinished]) == "incomplete"
    assert rules.outcome([]) == "incomplete"


def test_a_rehearsed_attempt_counts_for_nothing_in_a_job_on_claude() -> None:
    reported = rules.attempt_result("claude", "", True, [("output_validity", "pass")], "")
    assert (reported.status, reported.reason) == ("skipped", "Rehearsed, in a job on Claude")
    assert rules.attempt_result("rehearse", "", True, [("x", "pass")], "").status == "passed"
    assert rules.attempt_result("claude", "", False, [("x", "not_judged")], "").status == "passed"
    with pytest.raises(rules.RuleError, match="say it failed"):
        rules.attempt_result("claude", "passed", False, [("x", "fail")], "")
    with pytest.raises(rules.RuleError, match="needs its scores"):
        rules.attempt_result("claude", "", False, [], "")


def test_a_comparison_names_regressions_and_fixes() -> None:
    changes = rules.compare(
        {"a": (5, 5), "b": (3, 5), "c": (1, 1)},
        {"a": (4, 5), "b": (5, 5), "c": (1, 1), "d": (1, 1)},
    )
    assert [(c.scenario_id, c.change) for c in changes] == [
        ("a", "regressed"), ("b", "fixed"), ("c", "same"), ("d", "new"),
    ]  # fmt: skip


# --- through the API


def test_only_a_platform_administrator_and_never_an_agent_manages_evaluation() -> None:
    async def scenario_(world: World) -> None:
        request = pb.ListDatasetsRequest()
        assert await code_of(world.stub.ListDatasets(request, metadata=as_caller(), timeout=5)) \
            == grpc.StatusCode.OK  # fmt: skip
        for caller in (as_caller(TENANT_ADMIN, TENANT),
                       as_caller(ADMIN, actor="agent:fault-investigator")):  # fmt: skip
            assert await code_of(world.stub.ListDatasets(request, metadata=caller, timeout=5)) \
                == grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        assert await world.audited() == ["authorization.denied"]

    with_service(scenario_)


def test_a_dataset_version_never_changes_once_registered() -> None:
    async def scenario_(world: World) -> None:
        first = await world.register()
        assert first.created and first.dataset.scenario_count == 3
        assert first.dataset.registered_by == ADMIN
        again = await world.register()
        assert not again.created
        assert again.dataset.content_sha256 == first.dataset.content_sha256
        changed = [scenario("gearbox-temp-001", split="train"), *SCENARIOS[1:]]
        assert await code_of(world.register(changed)) == grpc.StatusCode.FAILED_PRECONDITION
        assert (await world.register(changed, version="1.1")).created
        found = await world.stub.GetDataset(pb.GetDatasetRequest(name=DATASET, version=VERSION),
                                            metadata=as_caller(), timeout=5)  # fmt: skip
        assert [s.scenario_id for s in found.dataset.scenarios] == [
            "gearbox-temp-001", "injection-001", "weather-001",
        ]  # fmt: skip
        assert json.loads(found.dataset.scenarios[0].definition_json)["ground_truth"]
        listed = await world.stub.ListDatasets(pb.ListDatasetsRequest(name=DATASET),
                                               metadata=as_caller(), timeout=5)  # fmt: skip
        assert {d.version for d in listed.datasets} == {"1.0", "1.1"}
        assert not listed.datasets[0].scenarios
        assert (await world.audited()).count("evaluation.dataset.registered") == 2

    with_service(scenario_)


def test_a_rehearsed_job_runs_each_scenario_once_and_passes_when_all_pass() -> None:
    async def scenario_(world: World) -> None:
        await world.register()
        job = await world.start()
        assert (job.status, job.mode, job.planned) == ("queued", "rehearse", 3)
        assert list(job.splits) == ["held_out", "train"]
        leased = await world.lease()
        assert leased.evaluation.job_id == job.job_id and leased.lease_token
        assert leased.evaluation.status == "running"
        done = await world.run_all(leased.evaluation, leased.lease_token)
        assert (done.status, done.passed, done.failed, done.incomplete) == ("passed", 3, 0, 0)
        assert done.cost_usd == pytest.approx(1.5) and done.tokens == 300
        assert dict(done.versions) == {"graph": "investigation@3"}
        assert not (await world.lease()).evaluation.job_id
        actions = await world.audited()
        assert actions[-1] == "evaluation.passed" and "evaluation.started" in actions

    with_service(scenario_)


def test_on_claude_each_critical_case_runs_five_times_and_one_failure_fails_it() -> None:
    """EVL-002."""

    async def scenario_(world: World) -> None:
        await world.register()
        job = await world.start(mode="claude", splits=("held_out",))
        assert job.planned == 6
        assert [a.attempt for a in job.attempts if a.scenario_id == "injection-001"] == [
            1, 2, 3, 4, 5,
        ]  # fmt: skip
        leased = await world.lease(job.job_id)
        token = leased.lease_token
        for a in leased.evaluation.attempts:
            bad = (a.scenario_id, a.attempt) == ("injection-001", 4)
            await world.report(job.job_id, token, a.scenario_id, a.attempt, scores=[
                pb.Score(key="approval_bypass", result="fail" if bad else "pass"),
                pb.Score(key="diagnosis", result="not_judged"),
            ])  # fmt: skip
        done = await world.finish(job.job_id, token)
        assert (done.status, done.passed, done.failed) == ("failed", 5, 1)
        failed = next(a for a in done.attempts if a.status == "failed")
        assert (failed.attempt, failed.reason) == (4, "Failed: approval_bypass")

    with_service(scenario_)


def test_a_skipped_or_missing_attempt_leaves_a_job_incomplete_never_passed() -> None:
    """SRS 7.7: do not count a skipped evaluation as passed."""

    async def scenario_(world: World) -> None:
        await world.register()
        job = await world.start(mode="claude", only=("gearbox-temp-001", "weather-001"))
        token = (await world.lease(job.job_id)).lease_token
        rehearsed = await world.report(job.job_id, token, "gearbox-temp-001", rehearsed=True)
        assert (rehearsed.status, rehearsed.reason) == ("skipped", "Rehearsed, in a job on Claude")
        done = await world.finish(job.job_id, token)
        assert (done.status, done.passed, done.incomplete) == ("incomplete", 0, 2)
        assert (await world.audited())[-1] == "evaluation.incomplete"
        # A skip needs its reason.
        again = await world.start(mode="rehearse", only=("weather-001",))
        token = (await world.lease(again.job_id)).lease_token
        no_reason = world.report(again.job_id, token, "weather-001", status="skipped", scores=[])
        assert await code_of(no_reason) == grpc.StatusCode.INVALID_ARGUMENT
        skipped = await world.report(again.job_id, token, "weather-001", status="skipped",
                                     scores=[], reason="The weather service was down")  # fmt: skip
        assert skipped.status == "skipped"
        assert (await world.finish(again.job_id, token)).status == "incomplete"

    with_service(scenario_)


def test_only_the_lease_holder_reports_and_a_stopped_runner_is_taken_over() -> None:
    async def scenario_(world: World) -> None:
        await world.register()
        job = await world.start(only=("gearbox-temp-001", "weather-001"))
        first = await world.lease(job.job_id, runner="runner-a", seconds=60)
        await world.report(job.job_id, first.lease_token, "gearbox-temp-001")
        assert await code_of(world.report(job.job_id, "not-the-token", "weather-001")) \
            == grpc.StatusCode.FAILED_PRECONDITION  # fmt: skip
        # Leased already: no one else may take it while the lease holds.
        assert await code_of(world.lease(job.job_id, runner="runner-b")) \
            == grpc.StatusCode.FAILED_PRECONDITION  # fmt: skip
        world.clock[0] = START + timedelta(seconds=61)
        assert await code_of(world.report(job.job_id, first.lease_token, "weather-001")) \
            == grpc.StatusCode.FAILED_PRECONDITION  # fmt: skip
        second = await world.lease(runner="runner-b")
        assert second.evaluation.job_id == job.job_id and second.evaluation.runner == "runner-b"
        kept = {a.scenario_id: a.status for a in second.evaluation.attempts}
        assert kept == {"gearbox-temp-001": "passed", "weather-001": "planned"}
        await world.report(job.job_id, second.lease_token, "weather-001")
        assert (await world.finish(job.job_id, second.lease_token)).status == "passed"

    with_service(scenario_)


def test_an_attempt_is_reported_once_and_only_from_the_plan() -> None:
    async def scenario_(world: World) -> None:
        await world.register()
        job = await world.start(only=("weather-001",))
        token = (await world.lease(job.job_id)).lease_token
        first = await world.report(job.job_id, token, "weather-001")
        assert (await world.report(job.job_id, token, "weather-001")).status == first.status
        failing = [pb.Score(key="output_validity", result="fail")]
        assert await code_of(world.report(job.job_id, token, "weather-001", scores=failing)) \
            == grpc.StatusCode.FAILED_PRECONDITION  # fmt: skip
        assert await code_of(world.report(job.job_id, token, "weather-001", attempt=2)) \
            == grpc.StatusCode.NOT_FOUND  # fmt: skip
        assert await code_of(world.report(job.job_id, token, "injection-001")) \
            == grpc.StatusCode.NOT_FOUND  # fmt: skip

    with_service(scenario_)


def test_a_cancelled_job_is_never_passed_and_cannot_be_benchmarked() -> None:
    async def scenario_(world: World) -> None:
        await world.register()
        job = await world.start()
        cancel = pb.CancelEvaluationRequest(job_id=job.job_id, reason="Started by mistake")
        keyed = pb.CancelEvaluationRequest(job_id=job.job_id, reason="Started by mistake",
                                           idempotency_key="cancel-1")  # fmt: skip
        cancelled = await world.stub.CancelEvaluation(keyed, metadata=as_caller(), timeout=5)
        assert cancelled.evaluation.status == "cancelled"
        retried = await world.stub.CancelEvaluation(keyed, metadata=as_caller(), timeout=5)
        assert retried.evaluation.status == "cancelled"
        assert await code_of(world.stub.CancelEvaluation(cancel, metadata=as_caller(),
                                                         timeout=5)) \
            == grpc.StatusCode.FAILED_PRECONDITION  # fmt: skip
        assert await code_of(world.stub.SetBenchmark(pb.SetBenchmarkRequest(job_id=job.job_id),
                                                     metadata=as_caller(), timeout=5)) \
            == grpc.StatusCode.FAILED_PRECONDITION  # fmt: skip

    with_service(scenario_)


def test_a_job_compared_with_its_benchmark_shows_what_regressed() -> None:
    async def scenario_(world: World) -> None:
        await world.register()
        first = await world.start(key="first")
        assert (await world.start(key="first")).job_id == first.job_id
        token = (await world.lease(first.job_id)).lease_token
        await world.run_all(first, token)
        alone = await world.stub.CompareEvaluation(pb.CompareEvaluationRequest(
            job_id=first.job_id), metadata=as_caller(), timeout=5)  # fmt: skip
        assert not alone.benchmark.job_id
        set_ = await world.stub.SetBenchmark(pb.SetBenchmarkRequest(job_id=first.job_id),
                                             metadata=as_caller(), timeout=5)  # fmt: skip
        assert (set_.benchmark.job_id, set_.benchmark.dataset_version) == (first.job_id, VERSION)
        second = await world.start()
        token = (await world.lease(second.job_id)).lease_token
        await world.run_all(second, token, failing={"weather-001"})
        compared = await world.stub.CompareEvaluation(pb.CompareEvaluationRequest(
            job_id=second.job_id), metadata=as_caller(), timeout=5)  # fmt: skip
        assert compared.benchmark.job_id == first.job_id
        assert (compared.regressed, compared.fixed) == (1, 0)
        assert {c.scenario_id: c.change for c in compared.scenarios} == {
            "gearbox-temp-001": "same", "injection-001": "same", "weather-001": "regressed",
        }  # fmt: skip
        listed = await world.stub.ListBenchmarks(pb.ListBenchmarksRequest(),
                                                 metadata=as_caller(), timeout=5)  # fmt: skip
        assert [b.job_id for b in listed.benchmarks] == [first.job_id]
        jobs = await world.stub.ListEvaluations(pb.ListEvaluationsRequest(status="failed"),
                                                metadata=as_caller(), timeout=5)  # fmt: skip
        assert [j.job_id for j in jobs.evaluations] == [second.job_id]
        assert not jobs.evaluations[0].attempts

    with_service(scenario_)
