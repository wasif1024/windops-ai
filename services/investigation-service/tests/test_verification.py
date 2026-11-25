"""Step 15: completed work is verified, and a person closes or reopens the case (M10.6)."""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from test_decisions import decided, decision_row, finish, pending_review
from test_investigations import Setup, case_of, ts
from test_windops_investigation import NOW, SECRET, TENANT, World, as_user, code_of
from windops.events.v1 import events_pb2
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.investigation.v1 import investigation_pb2 as pb
from windops.telemetry.v1 import telemetry_pb2 as tm
from windops_auth import VerifiedIdentity, sign_identity
from windops_events import Event, InvalidEventError
from windops_grpc import UnavailableError, current_identity
from windops_investigation import decisions, verification
from windops_investigation.store import EVIDENCE_ITEMS, VERIFICATIONS
from windops_investigation.verification import Finding, judge

WORK_ORDER = "wo_" + "2" * 20
WINDOW = timedelta(hours=1)
COMPLETED_AT = NOW + timedelta(hours=3)
BEARING, POWER = tm.METRIC_BEARING_TEMPERATURE, tm.METRIC_POWER


def summary(metric: tm.Metric.ValueType, mean: float, maximum: float, good: int) -> Any:
    return tm.MetricSummary(metric=metric, unit="degC" if metric == BEARING else "kW",
                            good=good, readings=good, mean=mean, maximum=maximum)  # fmt: skip


@dataclass
class FakeSources:
    """The telemetry and the fault rules, as the verifier asks them."""

    findings: list[Any] = field(default_factory=list)
    samples: int = 360
    down: bool = False
    callers: list[str] = field(default_factory=list)
    windows: list[tuple[datetime, datetime, str]] = field(default_factory=list)

    def _ask(self) -> None:
        self.callers.append(current_identity().subject)
        if self.down:
            raise UnavailableError("Telemetry cannot be reached right now")

    async def telemetry_snapshot(self, turbine_id: str, start: datetime, end: datetime,
                                 key: str) -> Any:  # fmt: skip
        self._ask()
        self.windows.append((start, end, key))
        return tm.TelemetrySnapshot(snapshot_id="tsnap_after", sample_count=self.samples)

    async def summarize(self, snapshot_id: str) -> list[Any]:
        self._ask()
        if snapshot_id == "tsnap_before":
            return [summary(BEARING, 92.5, 104.0, 700), summary(POWER, 1800.0, 2000.0, 700)]
        return [summary(BEARING, 61.25, 66.0, 340)]

    async def evaluate(self, turbine_id: str, at: datetime) -> Any:
        self._ask()
        return fd.EvaluateTurbineResponse(turbine_id=turbine_id, findings=self.findings,
                                          sample_count=self.samples)  # fmt: skip


def completed(case_id: str, snapshot: str, *, version: str = "1", producer: str = "maintenance",
              **changed: Any) -> Event:  # fmt: skip
    payload = events_pb2.WorkOrderCompleted(
        work_order_id=WORK_ORDER, completed_at=ts(COMPLETED_AT), case_id=case_id,
        turbine_id="dogger-east-t01", proposal_version=version, evidence_snapshot_id=snapshot,
        completed_by="u-planner", started_at=ts(COMPLETED_AT - timedelta(hours=2)),
    )  # fmt: skip
    for name, value in changed.items():
        setattr(payload, name, value)
    return Event("workorder.completed", f"e-done-{version}", TENANT, NOW, producer, payload, "",
                 "")  # fmt: skip


async def approved(world: World, setup: Setup) -> tuple[str, str]:
    """A case approved on its snapshot, whose evidence holds telemetry: (case, snapshot)."""
    case_id, snapshot, _ = await pending_review(world, setup)
    await decisions.on_decided(world.engine, decided(case_id, snapshot))
    keeper = setup.keeper(world)
    await keeper.run_once()
    finish(setup, (await decision_row(world)).run_id)
    await keeper.run_once()
    assert (await case_of(world, case_id)).case.status == "approved"
    async with world.engine.begin() as connection:
        await connection.execute(EVIDENCE_ITEMS.insert().values(
            tenant_id=TENANT, snapshot_id=snapshot, position=1, source_service="telemetry",
            record_type="telemetry_snapshot", record_id="tsnap_before", version="h",
            retrieved_at=NOW, data_quality="{}", content="{}", content_hash="x",
        ))  # fmt: skip
    return case_id, snapshot


async def row(world: World) -> Any:
    async with world.engine.connect() as connection:
        return (await connection.execute(select(VERIFICATIONS))).one()


def review(case_id: str, version: int, decision: str = "close", reason: str = "",
           key: str = "") -> pb.ReviewVerificationRequest:  # fmt: skip
    return pb.ReviewVerificationRequest(case_id=case_id, decision=decision, reason=reason,
                                        expected_version=version, idempotency_key=key)  # fmt: skip


def test_completed_work_is_verified_after_its_window_and_a_person_closes_the_case() -> None:
    setup, sources = Setup(), FakeSources()

    async def scenario(world: World) -> None:
        case_id, snapshot = await approved(world, setup)
        assert await verification.on_completed(world.engine, completed(case_id, snapshot), WINDOW)
        assert not await verification.on_completed(
            world.engine, completed(case_id, snapshot), WINDOW
        ), "a repeat changes nothing (EVT-003)"
        got = await case_of(world, case_id)
        assert (got.case.status, got.history[-1].action) == ("verifying", "maintenance_completed")
        assert got.history[-1].actor_id == "u-planner"
        [waiting] = got.verifications
        assert (waiting.status, waiting.outcome, waiting.completed_by) == (
            "waiting",
            "",
            "u-planner",
        )

        verifier = verification.Verifier(world.engine, sources, SECRET)
        assert await verifier.run_once(COMPLETED_AT + WINDOW) == 0, "not before the window settles"
        assert not sources.callers
        assert await verifier.run_once(COMPLETED_AT + WINDOW + timedelta(minutes=1)) == 1
        assert set(sources.callers) == {"service:investigation"}, "asked as this service"
        assert sources.windows == [(COMPLETED_AT, COMPLETED_AT + WINDOW, f"verify-{WORK_ORDER}-v1")]

        got = await case_of(world, case_id)
        assert (got.case.status, got.history[-1].action) == ("verified", "verification_reported")
        [done] = got.verifications
        assert (done.status, done.outcome, done.after_samples) == ("reported", "resolved", 360)
        assert (done.before_telemetry_snapshot_id, done.after_telemetry_snapshot_id) == (
            "tsnap_before", "tsnap_after")  # fmt: skip
        bearing, power = done.metrics
        assert (bearing.metric, bearing.before_mean, bearing.after_mean) == (
            "METRIC_BEARING_TEMPERATURE", 92.5, 61.25)  # fmt: skip
        assert (bearing.before_readings, bearing.after_readings) == (700, 340)
        assert power.HasField("before_mean") and not power.HasField("after_mean")
        updated = (await world.events("case.updated"))[-1]
        assert (updated.action, updated.work_order_id, updated.verification_outcome) == (
            "verification_reported", WORK_ORDER, "resolved")  # fmt: skip

        # Only someone who may review findings answers it, and never an agent (SEC-009).
        version = got.case.version
        assert await code_of(world.stub.ReviewVerification(
            review(case_id, version), metadata=as_user("u-operator"), timeout=5
        )) == grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        agent = VerifiedIdentity("u-engineer", TENANT, user_id="u-engineer",
                                 auth_time=int(time.time()), actor="agent:supervisor")  # fmt: skip
        assert await code_of(world.stub.ReviewVerification(
            review(case_id, version), timeout=5,
            metadata=(("x-windops-identity", sign_identity(agent, SECRET)),),
        )) == grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        assert await code_of(world.stub.ReviewVerification(
            review(case_id, version - 1), metadata=as_user("u-engineer"), timeout=5
        )) == grpc.StatusCode.FAILED_PRECONDITION, "on the version the person saw"  # fmt: skip
        answer = await world.stub.ReviewVerification(
            review(case_id, version, key="close-1"), metadata=as_user("u-engineer"), timeout=5
        )
        assert answer.case.status == "closed" and answer.verification.review == "closed"
        assert answer.verification.reviewed_by == "u-engineer"
        again = await world.stub.ReviewVerification(
            review(case_id, version, key="close-1"), metadata=as_user("u-engineer"), timeout=5
        )
        assert again == answer, "a retry answers the same (GRP-007)"
        assert await code_of(world.stub.ReviewVerification(
            review(case_id, answer.case.version), metadata=as_user("u-engineer"), timeout=5
        )) == grpc.StatusCode.FAILED_PRECONDITION, "closed: not reviewed again"  # fmt: skip

    setup.run(scenario, sources=sources)


def test_a_person_reopens_a_case_whose_fault_is_still_found() -> None:
    setup = Setup()
    sources = FakeSources(findings=[fd.Finding(rule_id="bearing-temp", version=2,
                                               kind="threshold", severity="high",
                                               summary="Still hot")])  # fmt: skip

    async def scenario(world: World) -> None:
        case_id, snapshot = await approved(world, setup)
        await verification.on_completed(world.engine, completed(case_id, snapshot), WINDOW)
        await verification.Verifier(world.engine, sources, SECRET).run_once(NOW + timedelta(days=1))
        got = await case_of(world, case_id)
        [done] = got.verifications
        assert done.outcome == "not_resolved" and "bearing-temp" in done.note
        assert [(r.rule_id, r.counts) for r in done.rules] == [("bearing-temp", True)], (
            "every rule counts for a case a person opened"
        )
        assert await code_of(world.stub.ReviewVerification(
            review(case_id, got.case.version, "reopen"), metadata=as_user("u-engineer"), timeout=5
        )) == grpc.StatusCode.INVALID_ARGUMENT, "reopening needs a reason"  # fmt: skip
        answer = await world.stub.ReviewVerification(
            review(case_id, got.case.version, "reopen", "Still hot after the bearing change"),
            metadata=as_user("u-engineer"), timeout=5,
        )  # fmt: skip
        assert answer.case.status == "open" and answer.verification.review == "reopened"

    setup.run(scenario, sources=sources)


def test_work_on_a_case_no_longer_approved_is_kept_but_not_verified() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, snapshot, _ = await pending_review(world, setup)
        assert await verification.on_completed(world.engine, completed(case_id, snapshot), WINDOW)
        got = await case_of(world, case_id)
        assert got.case.status == "pending_review"
        [kept] = got.verifications
        assert kept.status == "not_started" and "pending_review" in kept.note
        assert not await verification.on_completed(
            world.engine, completed("case_gone", snapshot, version="2"), WINDOW
        ), "a case this service does not have"
        assert (
            await verification.Verifier(world.engine, FakeSources(), SECRET).run_once(
                NOW + timedelta(days=1)
            )
            == 0
        )

    setup.run(scenario)


def test_an_owner_that_is_down_delays_the_verification_and_a_day_gives_up() -> None:
    setup, sources = Setup(), FakeSources(down=True)

    async def scenario(world: World) -> None:
        case_id, snapshot = await approved(world, setup)
        await verification.on_completed(world.engine, completed(case_id, snapshot), WINDOW)
        verifier = verification.Verifier(world.engine, sources, SECRET)
        later = COMPLETED_AT + WINDOW + timedelta(minutes=1)
        assert await verifier.run_once(later) == 0
        waiting = await row(world)
        assert (waiting.status, waiting.attempts) == ("waiting", 1)
        assert "UnavailableError" in waiting.note
        assert await verifier.run_once(later) == 0, "not again until it is due"
        assert len(sources.callers) == 1
        assert await verifier.run_once(COMPLETED_AT + WINDOW + timedelta(days=1, minutes=1)) == 1
        done = await row(world)
        assert (done.status, done.outcome) == ("reported", "inconclusive")
        assert "within a day" in done.note
        assert (await case_of(world, case_id)).case.status == "verified"

    setup.run(scenario, sources=sources)


def test_the_outcome_is_worked_out_from_the_rules_that_count() -> None:
    hot = Finding("gearbox-oil-residual", 1, "residual", "high", "Hot")
    gap = Finding("gearbox-oil-gap", 1, "data_gap", "medium", "Missing")
    other = Finding("pitch-misalignment", 1, "threshold", "medium", "Pitch")
    mine = {"gearbox-oil-residual", "gearbox-oil-gap"}
    assert judge(360, [], mine)[0] == "resolved"
    assert judge(360, [other], mine)[0] == "resolved", "another component's rule does not count"
    outcome, rules, note = judge(360, [hot, other], mine)
    assert outcome == "not_resolved" and note.endswith("gearbox-oil-residual")
    assert [r["counts"] for r in rules] == [True, False]
    assert judge(360, [gap, hot], mine)[0] == "inconclusive", "a data gap says too little"
    assert judge(9, [hot], mine)[0] == "inconclusive", "too few samples"
    assert judge(360, [other], set())[0] == "not_resolved", "every rule counts with none of its own"


def test_the_window_is_a_setting() -> None:
    assert verification.window_setting({}) == timedelta(hours=1)
    assert verification.window_setting({"WINDOPS_VERIFICATION_WINDOW_MINUTES": "2"}) == timedelta(
        minutes=2
    )
    for wrong in ("0", "1441", "an hour"):
        with pytest.raises(SystemExit):
            verification.window_setting({"WINDOPS_VERIFICATION_WINDOW_MINUTES": wrong})


def test_only_the_maintenance_service_may_say_work_was_completed() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        with pytest.raises(InvalidEventError):
            await verification.on_completed(
                world.engine, completed("case_x", "snap", producer="approval"), WINDOW
            )
        with pytest.raises(InvalidEventError):
            await verification.on_completed(
                world.engine, completed("case_x", "snap", completed_by=""), WINDOW
            )
        async with world.engine.connect() as connection:
            assert not (await connection.execute(select(VERIFICATIONS))).all()

    setup.run(scenario)
