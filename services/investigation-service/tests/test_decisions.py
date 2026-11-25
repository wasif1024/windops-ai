"""Step 14: a person's decision resumes the case's workflow, from approval.decided (M10.4)."""

import json
from typing import Any

import pytest
from sqlalchemy import select
from test_investigations import Setup, case_of, freeze, investigate, ts, watch
from test_windops_investigation import NOW, TENANT, World
from windops.agent_runtime.v1 import agent_runtime_pb2 as rt
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError
from windops_investigation import decisions
from windops_investigation.store import CASE_DECISIONS

APPROVAL = "apr_" + "1" * 20
WORK_ORDER = "wo_" + "2" * 20
DECISION_STEPS = (
    "receive_decision", "check_compatibility", "restore", "record_references", "conclude",
)  # fmt: skip


APPROVED = events_pb2.APPROVAL_DECISION_APPROVED


def decided(case_id: str, snapshot_id: str, *, approval: str = APPROVAL,
            decision: events_pb2.ApprovalDecision.ValueType = APPROVED,
            producer: str = "approval", **changed: Any) -> Event:  # fmt: skip
    payload = events_pb2.ApprovalDecided(
        approval_id=approval, work_order_id=WORK_ORDER, proposal_version="1",
        evidence_snapshot_id=snapshot_id, decision=decision, decided_by="u-manager",
        case_id=case_id,
    )  # fmt: skip
    for name, value in changed.items():
        setattr(payload, name, value)
    return Event("approval.decided", f"e-{approval}", TENANT, NOW, producer, payload, "", "")


def finish(setup: Setup, run_id: str, status: str = "completed", reason: str = "",
           result: dict[str, Any] | None = None) -> None:  # fmt: skip
    run = setup.runtime.runs[run_id]
    for position, name in enumerate(DECISION_STEPS, start=1):
        if status == "completed" or position <= 1:
            run.steps.append(rt.Step(position=position, name=name, attempt=1,
                                     finished_at=ts(NOW)))  # fmt: skip
    run.status, run.reason = status, reason
    run.result_json = json.dumps(result or {})


async def pending_review(world: World, setup: Setup) -> tuple[str, str, str]:
    """A case whose investigation completed at step 13: (case, snapshot, investigation run)."""
    keeper = setup.keeper(world)
    case = await investigate(world)
    await freeze(world, case.case_id)
    await keeper.run_once()
    run_id = (await case_of(world, case.case_id)).case.run_id
    setup.runtime.advance(run_id, 13, "completed")
    await keeper.run_once()
    got = await case_of(world, case.case_id)
    assert got.case.status == "pending_review"
    return case.case_id, got.case.evidence_snapshot_id, run_id


async def decision_row(world: World) -> Any:
    async with world.engine.connect() as connection:
        return (await connection.execute(select(CASE_DECISIONS))).one()


def test_an_approval_resumes_the_investigation_and_the_case_is_approved() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, snapshot, investigation = await pending_review(world, setup)
        assert await decisions.on_decided(world.engine, decided(case_id, snapshot))
        assert not await decisions.on_decided(world.engine, decided(case_id, snapshot)), (
            "a repeat changes nothing (EVT-003)"
        )
        keeper = setup.keeper(world)
        assert await keeper.run_once() == 1
        started = setup.runtime.started[-1]
        assert started["workflow"] == "decision" and started["delegation_id"] == ""
        assert started["key"] == f"decide-{APPROVAL}" and started["snapshot_id"] == snapshot
        assert started["parameters"] == {
            "resumes": investigation, "approval_id": APPROVAL, "decision": "approved",
            "work_order_id": WORK_ORDER, "proposal_version": "1", "decided_by": "u-manager",
        }  # fmt: skip
        run_id = (await decision_row(world)).run_id
        assert (await case_of(world, case_id)).case.status == "pending_review", "until it ends"

        finish(setup, run_id, result={"approval_id": APPROVAL, "resumed_run_id": investigation})
        assert await keeper.run_once() == 1
        assert await keeper.run_once() == 0, "followed to its end once"
        got = await case_of(world, case_id)
        assert got.case.status == "approved"
        assert got.history[-1].action == "approved" and APPROVAL in got.history[-1].note
        row = await decision_row(world)
        assert (row.status, row.resumes_run_id) == ("completed", investigation)
        assert json.loads(row.references_json)["resumed_run_id"] == investigation

        events = [s.event for s in (await watch(world, case_id))[:-1]]
        tail = [(e.kind, e.step or e.status) for e in events][-9:]
        assert tail == [
            ("decided", "approved"), ("started", ""),
            *[("step", name) for name in DECISION_STEPS],
            ("run_ended", "completed"), ("case_status", "approved"),
        ][-9:]  # fmt: skip
        # Notification hears of it on case.updated (M10.5).
        updated = await world.events("case.updated")
        assert (updated[-1].action, updated[-1].status) == ("approved", "approved")
        # Who to tell: the person who opened the case (M10.5).
        assert updated[-1].requested_by == "u-operator"

    setup.run(scenario)


def test_a_rejection_keeps_the_case_waiting_for_a_revised_proposal() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, snapshot, _ = await pending_review(world, setup)
        rejected = decided(case_id, snapshot, decision=events_pb2.APPROVAL_DECISION_REJECTED)
        await decisions.on_decided(world.engine, rejected)
        keeper = setup.keeper(world)
        await keeper.run_once()
        finish(setup, (await decision_row(world)).run_id)
        await keeper.run_once()
        got = await case_of(world, case_id)
        assert (got.case.status, got.history[-1].action) == ("pending_review", "rejected")

    setup.run(scenario)


def test_an_incompatible_resume_sends_the_case_back_to_be_restarted() -> None:
    """AGT-005: the run stops safely, and a person restarts from the snapshot."""
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, snapshot, _ = await pending_review(world, setup)
        await decisions.on_decided(world.engine, decided(case_id, snapshot))
        keeper = setup.keeper(world)
        await keeper.run_once()
        why = (
            "Its graph, prompt, tool or skill versions changed: restart the case from its snapshot"
        )
        finish(setup, (await decision_row(world)).run_id, "stale", why)
        await keeper.run_once()
        got = await case_of(world, case_id)
        assert (got.case.status, got.history[-1].action) == ("open", "decision_not_resumed")
        assert "restart" in got.history[-1].note and APPROVAL in got.history[-1].note
        row = await decision_row(world)
        assert (row.status, row.references_json) == ("ended", "")

    setup.run(scenario)


def test_a_decision_with_no_investigation_on_its_snapshot_is_recorded_without_a_run() -> None:
    """As for a person's own proposal: nothing to resume, and the case still moves."""
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, _, _ = await pending_review(world, setup)
        asked = len(setup.runtime.started)
        # Decided on a snapshot the investigation never ran on, though still the case's.
        from sqlalchemy import update
        from windops_investigation.store import CASES

        async with world.engine.begin() as connection:
            await connection.execute(update(CASES).values(evidence_snapshot_id="evs_newer"))
        await decisions.on_decided(world.engine, decided(case_id, "evs_newer"))
        await setup.keeper(world).run_once()
        assert len(setup.runtime.started) == asked, "no run is started"
        assert (await decision_row(world)).status == "recorded"
        assert (await case_of(world, case_id)).case.status == "approved"

    setup.run(scenario)


def test_a_decision_on_older_evidence_does_not_move_the_case() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, _, _ = await pending_review(world, setup)
        await decisions.on_decided(world.engine, decided(case_id, "evs_older"))
        await setup.keeper(world).run_once()
        assert (await decision_row(world)).status == "recorded"
        assert (await case_of(world, case_id)).case.status == "pending_review"

    setup.run(scenario)


def test_a_decision_waits_while_the_cases_investigation_is_running() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        keeper = setup.keeper(world)
        case = await investigate(world)
        await freeze(world, case.case_id)
        await keeper.run_once()
        snapshot = (await case_of(world, case.case_id)).case.evidence_snapshot_id
        await decisions.on_decided(world.engine, decided(case.case_id, snapshot))
        await keeper.run_once()
        assert (await decision_row(world)).status == "waiting"
        assert len(setup.runtime.started) == 1

    setup.run(scenario)


def test_only_the_approval_service_may_announce_a_decision() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, snapshot, _ = await pending_review(world, setup)
        with pytest.raises(InvalidEventError):
            await decisions.on_decided(world.engine, decided(case_id, snapshot,
                                                             producer="maintenance"))  # fmt: skip
        with pytest.raises(InvalidEventError):
            await decisions.on_decided(world.engine, decided(case_id, snapshot, decided_by=""))
        # From before decisions named their case, or for a case not here: ignored, not dead.
        assert not await decisions.on_decided(world.engine, decided("", snapshot))
        assert not await decisions.on_decided(world.engine, decided("case_gone", snapshot))

    setup.run(scenario)
