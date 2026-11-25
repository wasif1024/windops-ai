"""The polled stale rules (SRS 8.5, M10.3b): condition drift and weather, each on its own, then
applied to a real case by the watcher, as the Investigation Service."""

from datetime import timedelta
from typing import Any

from sqlalchemy import update
from test_evidence import NOW, TENANT, TURBINE, Bench, ts, with_bench
from test_stale import ORDER, announced, frozen_case
from windops.events.v1 import events_pb2
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.policy.v1 import policy_pb2 as pol
from windops_events import Event
from windops_grpc import UnavailableError, current_identity
from windops_investigation import stale, staleness, watch
from windops_investigation.staleness import Figure
from windops_investigation.store import CASES

SECRET = "x" * 32
SUBMITTED_RESULT = "pol_" + "s" * 20


def test_drift_is_a_rules_figure_moving_beyond_its_tolerance() -> None:
    frozen = {"gearbox-oil-residual": Figure("residual", 4.0)}
    assert staleness.drift(frozen, {"gearbox-oil-residual": Figure("residual", 5.9)}) is None
    found = staleness.drift(frozen, {"gearbox-oil-residual": Figure("residual", 6.5)})
    assert found is not None and found.rule == "drift" and "4.00" in found.detail
    assert staleness.drift(frozen, {"gearbox-oil-residual": Figure("residual", 1.5)}) is not None
    assert staleness.drift(frozen, {}) is None, "a rule that no longer finds anything"
    small = {"r": Figure("threshold", 0.4)}
    assert staleness.drift(small, {"r": Figure("threshold", 1.3)}) is None, "a floor of 1"
    assert staleness.drift(small, {"r": Figure("threshold", 1.5)}) is not None
    gap = {"g": Figure("data_gap", 10.0)}
    assert staleness.drift(gap, {"g": Figure("data_gap", 90.0)}) is None, "no figure that drifts"


def test_weather_is_a_work_window_rule_doing_worse_than_at_submission() -> None:
    before = {"work-at-height-wind": "pass", "work-at-height-gusts": "pass"}
    assert staleness.weather(before, [("work-at-height-wind", "pass", "")], ORDER) is None
    found = staleness.weather(before, [("work-at-height-wind", "block", "Wind 15 > 12")], ORDER)
    assert found is not None and found.rule == "weather"
    assert "pass when submitted, block now. Wind 15 > 12" in found.detail
    escalated = {"work-at-height-wind": "escalate"}
    assert staleness.weather(escalated, [("work-at-height-wind", "pass", "")], ORDER) is None
    assert staleness.weather(escalated, [("work-at-height-wind", "block", "")], ORDER) is not None


class FakeOwners:
    def __init__(self) -> None:
        self.amount, self.wind, self.down = 4.0, "pass", False
        self.climb, self.planned_end = True, NOW + timedelta(days=2)
        self.callers: list[str] = []
        self.keys: list[str] = []
        self.rule_version = 1

    def _ask(self) -> None:
        self.callers.append(current_identity().subject)
        if self.down:
            raise UnavailableError("Fault Detection cannot be reached right now")

    async def evaluate(self, turbine_id: str, at: Any) -> Any:
        self._ask()
        return fd.EvaluateTurbineResponse(turbine_id=turbine_id, findings=[
            fd.Finding(rule_id="gearbox-oil-residual", version=1, kind="residual",
                       severity="high", amount=self.amount)])  # fmt: skip

    async def policy_rules(self) -> list[Any]:
        self._ask()
        version = pol.PolicyRuleVersion(version=self.rule_version)
        return [pol.PolicyRule(rule_id="work-at-height-wind", category="weather_window",
                               enabled=True, current=version)]  # fmt: skip

    def _result(self, outcome: str) -> Any:
        proposal = pol.Proposal(action="repair", requires_climb=self.climb,
                                planned_start=ts(NOW + timedelta(days=1)),
                                planned_end=ts(self.planned_end))  # fmt: skip
        return pol.PolicyResult(proposal=proposal, rules=[
            pol.RuleOutcome(rule_id="work-at-height-wind", category="weather_window",
                            outcome=outcome, reason=f"Wind is {outcome}"),
            pol.RuleOutcome(rule_id="impact-cost", category="business", outcome="block"),
        ])  # fmt: skip

    async def policy_result(self, result_id: str) -> Any:
        self._ask()
        assert result_id == SUBMITTED_RESULT
        return self._result("pass")

    async def evaluate_proposal(self, case_id: str, proposal: Any, impact_result_id: str,
                                key: str) -> Any:  # fmt: skip
        self._ask()
        assert impact_result_id == "imp_1" and proposal.requires_climb == self.climb
        self.keys.append(key)
        return self._result(self.wind)


def with_figure(bench: Bench, amount: float) -> None:
    """The fake Fault Detection's detections carry a residual figure."""
    plain = bench.sources.detection

    async def detection(detection_id: str) -> Any:
        found = await plain(detection_id)
        found.detected_at.CopyFrom(ts(NOW))
        found.kind, found.amount = "residual", amount
        return found

    bench.sources.detection = detection  # type: ignore[method-assign]


async def submit(bench: Bench, case: Any, policy_result_id: str = SUBMITTED_RESULT) -> None:
    payload = events_pb2.WorkOrderSubmitted(
        work_order_id=ORDER, proposal_version="1", case_id=case.case_id, turbine_id=TURBINE,
        evidence_snapshot_id=case.evidence_snapshot_id, submitted_by="u-planner",
        policy_result_id=policy_result_id, impact_result_id="imp_1",
    )  # fmt: skip
    await stale.on_submitted(bench.engine, Event("workorder.submitted", "s-1", TENANT, NOW,
                                                 "maintenance", payload, "", ""))  # fmt: skip


def test_a_case_goes_stale_when_its_fault_figure_drifts() -> None:
    owners = FakeOwners()

    async def scenario(bench: Bench) -> None:
        with_figure(bench, 4.0)
        case = await frozen_case(bench)
        watcher = watch.Watcher(bench.engine, owners, SECRET)
        assert await watcher.run_once(NOW) == 0, "within the tolerance"
        assert owners.callers == ["service:investigation"], "asked as this service"
        owners.down = True
        assert await watcher.run_once(NOW) == 0, "an owner down: judged at the next pass"
        owners.down, owners.amount = False, 9.0
        assert await watcher.run_once(NOW) == 1
        got = await bench.case()
        assert (got.stale_rule, got.stale_rules_version) == ("drift", staleness.VERSION)
        assert "gearbox-oil-residual" in got.stale_detail
        [event] = await announced(bench)
        assert event.evidence_snapshot_id == case.evidence_snapshot_id
        assert await watcher.run_once(NOW) == 0, "stale once, until a new snapshot"

    with_bench(scenario)


def test_a_submitted_proposal_goes_stale_when_the_weather_breaches_its_window() -> None:
    owners = FakeOwners()

    async def scenario(bench: Bench) -> None:
        with_figure(bench, 4.0)
        case = await frozen_case(bench)
        await submit(bench, case)
        watcher = watch.Watcher(bench.engine, owners, SECRET)
        assert await watcher.run_once(NOW) == 0
        assert await watcher.run_once(NOW + timedelta(minutes=5)) == 0
        assert len(set(owners.keys)) == 1, "Policy is asked once an hour"
        owners.rule_version = 2
        await watcher.run_once(NOW + timedelta(minutes=10))
        assert len(set(owners.keys)) == 2, "and again when the work-window rules change"
        owners.wind = "block"
        assert await watcher.run_once(NOW + timedelta(hours=1)) == 1
        got = await bench.case()
        assert got.stale_rule == "weather" and ORDER in got.stale_detail
        assert "impact-cost" not in got.stale_detail, "only the work-window rules count"

    with_bench(scenario)


def test_weather_is_not_judged_without_work_at_height_or_after_its_window() -> None:
    async def scenario(bench: Bench) -> None:
        with_figure(bench, 4.0)
        case = await frozen_case(bench)
        await submit(bench, case)
        watcher = watch.Watcher(bench.engine, owners, SECRET)
        owners.wind, owners.climb = "block", False
        assert await watcher.run_once(NOW) == 0
        owners.climb, owners.planned_end = True, NOW - timedelta(hours=1)
        assert await watcher.run_once(NOW) == 0
        assert not owners.keys
        async with bench.engine.begin() as connection:
            await connection.execute(update(CASES).values(status="verifying"))
        owners.amount, owners.planned_end = 50.0, NOW + timedelta(days=2)
        assert await watcher.run_once(NOW) == 0, "once the work is done, verification judges it"

    owners = FakeOwners()
    with_bench(scenario)


def test_a_case_a_person_opened_has_no_figure_to_drift() -> None:
    assert watch.frozen_figures([]) == {}
