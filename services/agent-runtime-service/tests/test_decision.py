"""Step 14: a decision resumes the case's workflow from the investigation's checkpoint (M10.4)."""

import asyncio
import dataclasses
import json
from collections.abc import Mapping
from typing import Any
from unittest import mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from test_agent_runtime import (
    CASE,
    POLICY,
    SNAPSHOT,
    Clock,
    caller,
    get,
    scenario,
    start_investigation,
    steps,
)
from windops.agent_runtime.v1 import agent_runtime_pb2 as pb
from windops_agent_runtime.decision import STEPS, Decision, Resumed, build, incompatibility
from windops_agent_runtime.investigation import Investigation
from windops_agent_runtime.queue import Queue
from windops_agent_runtime.registry import REGISTRY
from windops_agent_runtime.service import INVESTIGATION, AgentRuntimeService
from windops_agent_runtime.worker import Worker
from windops_grpc import InvalidRequestError

APPROVAL = "apr_" + "1" * 20
WORK_ORDER = "wo_" + "2" * 20


def decision_parameters(resumes: str, **changed: str) -> dict[str, str]:
    return {
        "resumes": resumes, "approval_id": APPROVAL, "decision": "approved",
        "work_order_id": WORK_ORDER, "proposal_version": "1", "decided_by": "user:manager-1",
        **changed,
    }  # fmt: skip


async def start_decision(
    engine: AsyncEngine, clock: Clock, resumes: str, *, snapshot: str = SNAPSHOT,
    key: str = "decide-key-1", **changed: str,
) -> pb.Run:  # fmt: skip
    request = pb.StartRunRequest(
        case_id=CASE, snapshot_id=snapshot, workflow="decision",
        parameters=decision_parameters(resumes, **changed),
        requested_by="service:investigation", idempotency_key=key,
    )  # fmt: skip
    with caller():
        api = AgentRuntimeService(engine, REGISTRY, peer=lambda: INVESTIGATION, clock=clock)
        return (await api.StartRun(request, None)).run


async def investigated(engine: AsyncEngine, clock: Clock, queue: Queue) -> pb.Run:
    """A completed investigation of the case, ended at step 13 with its checkpoint saved."""
    run = await start_investigation(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    assert await Worker(queue, REGISTRY, policy=POLICY).execute(claim, asyncio.Event()) == (
        "completed"
    )
    return run


async def decide(queue: Queue, workflows: Mapping[str, Any] = REGISTRY) -> str:
    claim = await queue.claim("replica-a/0")
    assert claim is not None and claim.workflow == "decision"
    return await Worker(queue, workflows, policy=POLICY).execute(claim, asyncio.Event())


def test_the_decision_graph_has_its_steps_in_order_and_no_interrupt() -> None:
    assert STEPS == (
        "receive_decision", "check_compatibility", "restore", "record_references", "conclude",
    )  # fmt: skip
    graph = build(None, decision_parameters("run_" + "c" * 20)).compile()  # type: ignore[arg-type]
    # AGT-003: the decision is never waited for inside a graph.
    assert not graph.interrupt_before_nodes and not graph.interrupt_after_nodes


@pytest.mark.parametrize(
    "changed",
    [
        {"resumes": "run_short"},
        {"approval_id": "apr_nothex" + "z" * 12},
        {"decision": "maybe"},
        {"work_order_id": "wo_1"},
        {"proposal_version": "v1"},
        {"decided_by": "a person with spaces"},
    ],
)
def test_a_decisions_parameters_are_checked(changed: dict[str, str]) -> None:
    with pytest.raises(InvalidRequestError):
        Decision().check({**decision_parameters("run_" + "c" * 20), **changed})


def test_a_decision_needs_every_parameter_and_no_others() -> None:
    whole = decision_parameters("run_" + "c" * 20)
    with pytest.raises(InvalidRequestError, match="Missing"):
        Decision().check({k: v for k, v in whole.items() if k != "decided_by"})
    with pytest.raises(InvalidRequestError, match="Unknown"):
        Decision().check({**whole, "team": "true"})


@scenario
async def test_a_decision_resumes_the_completed_investigation_and_records_references(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    investigation = await investigated(engine, clock, queue)
    run = await start_decision(engine, clock, investigation.run_id)
    assert json.loads(run.pins_json)["graph"] == "decision@1"
    assert await decide(queue) == "completed"
    assert [s.name for s in await steps(engine, run.run_id)] == list(STEPS)
    kept = json.loads((await get(engine, clock, run.run_id)).result_json)
    assert kept["decision"] == "approved"
    assert kept["approval_id"] == APPROVAL and kept["work_order_id"] == WORK_ORDER
    assert kept["resumed_run_id"] == investigation.run_id and kept["snapshot_id"] == SNAPSHOT
    assert kept["decided_by"] == "user:manager-1"
    # Without its team the investigation recommended nothing, and drafted nothing.
    assert kept["policy_result_ids"] == [] and kept["drafted_by_investigation"] is False
    # The resumed run is read, never written: it is still as step 13 left it.
    resumed = await get(engine, clock, investigation.run_id)
    assert resumed.status == "completed" and len(resumed.steps) == 13


@scenario
async def test_a_decision_on_another_snapshot_stops_as_stale(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    investigation = await investigated(engine, clock, queue)
    run = await start_decision(engine, clock, investigation.run_id, snapshot="evs_" + "d" * 20)
    assert await decide(queue) == "stale"
    ended = await get(engine, clock, run.run_id)
    assert "another evidence snapshot" in ended.reason and "restart" in ended.reason
    assert ended.result_json == ""


@scenario
async def test_a_decision_never_resumes_a_run_on_other_pins(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    investigation = await investigated(engine, clock, queue)
    run = await start_decision(engine, clock, investigation.run_id)
    # The investigation graph changed since the run: its pins no longer match (AGT-004, AGT-005).
    from windops_agent_runtime.investigation import pins_of

    def reshaped(parameters: Mapping[str, Any]) -> dict[str, Any]:
        return {**pins_of(parameters), "graph_shape": "another"}

    with mock.patch("windops_agent_runtime.decision.pins_of", reshaped):
        assert await decide(queue) == "stale"
    assert "versions changed" in (await get(engine, clock, run.run_id)).reason


@scenario
async def test_a_decision_naming_a_run_that_is_not_there_stops_as_stale(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_decision(engine, clock, "run_" + "e" * 20)
    assert await decide(queue) == "stale"
    assert "not in this tenant" in (await get(engine, clock, run.run_id)).reason


def resumed(**changed: Any) -> Resumed:
    whole: dict[str, Any] = {
        "run_id": "run_" + "c" * 20, "case_id": CASE, "snapshot_id": SNAPSHOT,
        "workflow": "investigation", "workflow_version": Investigation.version,
        "status": "completed", "parameters": {"team": False, "plan_with_claude": False},
        "pins": {}, "policy_hash": "policy-1", "state": {"completed": ["conclude"]},
    }  # fmt: skip
    from windops_agent_runtime.investigation import pins_of

    whole["pins"] = pins_of(whole["parameters"])
    return dataclasses.replace(Resumed(**whole), **changed)


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        ({"case_id": "case_" + "f" * 20}, "another case"),
        ({"status": "failed"}, "not a completed investigation"),
        ({"workflow": "rehearsal"}, "not a completed investigation"),
        ({"workflow_version": "3"}, "no longer available"),
        ({"policy_hash": "policy-0"}, "Claude policy"),
        ({"state": {"completed": ["recommend"]}}, "does not end at step 13"),
        ({"state": None}, "does not end at step 13"),
    ],
)
def test_each_incompatibility_is_named(changed: dict[str, Any], why: str) -> None:
    found = incompatibility(resumed(**changed), case_id=CASE, snapshot_id=SNAPSHOT,
                            policy_hash="policy-1")  # fmt: skip
    assert why in found
    assert incompatibility(resumed(), case_id=CASE, snapshot_id=SNAPSHOT,
                           policy_hash="policy-1") == ""  # fmt: skip
