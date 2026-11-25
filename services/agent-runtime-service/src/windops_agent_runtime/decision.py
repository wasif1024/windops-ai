"""Step 14: a person's decision resumes the case's workflow, from a durable event (M10.4, ADR 0036).

    receive_decision -> check_compatibility -> restore -> record_references -> conclude

**Why a run of its own:** the investigation ends at step 13, with its checkpoint saved, and never
waits on a person (AGT-003). The Approval Service decides, and announces `approval.decided`. The
Investigation Service, which owns the case, hears it and asks for this run, naming the
investigation run it resumes. The decision itself stays in the Approval Service: this run only
carries the case's workflow on from it.

**Compatibility (AGT-004, AGT-005):** the resumed run must be this case's completed
investigation, on the decision's snapshot, pinned to the graph, prompt, tool and skill versions
and Claude policy this runtime would pin today. Anything else stops the run as `stale`, asking a
person to restart from the snapshot, and nothing is restored.

**Restore:** the resumed run's newest checkpoint, read and never written: its structured result
and its policy outcomes. **Record references:** the decision, the work order and version, and
what the investigation's result rested on, kept as this run's result. Investigation, following
this run, then moves the case and announces it, for the Notification Service (M10.5).

No agent acts here and nothing calls Claude, so the run has no delegation and costs nothing.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from windops_grpc import InvalidRequestError

from windops_agent_runtime.investigation import Investigation, canonical_pins, pins_of
from windops_agent_runtime.workflows import Context, StepFailedError

GRAPH_VERSION = "1"
STEPS = ("receive_decision", "check_compatibility", "restore", "record_references", "conclude")
DECISIONS = ("approved", "rejected")
_RUN = re.compile(r"run_[a-z0-9]{20}")
_APPROVAL = re.compile(r"apr_[a-f0-9]{20}")
_WORK_ORDER = re.compile(r"wo_[a-f0-9]{20}")
_PERSON = re.compile(r"[A-Za-z0-9._:@-]{1,160}")


class IncompatibleResumeError(Exception):
    """The resumed run cannot be carried on: the run stops as stale (AGT-005)."""


@dataclass(frozen=True)
class Resumed:
    """The run a decision resumes, as the runtime keeps it, and its newest checkpoint's state."""

    run_id: str
    case_id: str
    snapshot_id: str
    workflow: str
    workflow_version: str
    status: str
    parameters: dict[str, Any]
    pins: dict[str, Any]
    policy_hash: str
    state: dict[str, Any] | None


def incompatibility(
    resumed: Resumed | None, *, case_id: str, snapshot_id: str, policy_hash: str | None
) -> str:
    """Why the resumed run cannot be carried on, or "" when it can (AGT-004)."""
    if resumed is None:
        return "The run it resumes is not in this tenant"
    if resumed.case_id != case_id:
        return "The run it resumes belongs to another case"
    if resumed.workflow != Investigation.name or resumed.status != "completed":
        return "The run it resumes is not a completed investigation"
    if resumed.snapshot_id != snapshot_id:
        return "The run it resumes was on another evidence snapshot"
    if resumed.workflow_version != Investigation.version:
        return "Its investigation graph version is no longer available"
    if canonical_pins(pins_of(resumed.parameters)) != canonical_pins(resumed.pins):
        return "Its graph, prompt, tool or skill versions changed"
    if policy_hash is not None and resumed.policy_hash and resumed.policy_hash != policy_hash:
        return "Its Claude policy version is no longer in force"
    if not resumed.state or resumed.state.get("completed", [])[-1:] != ["conclude"]:
        return "Its checkpoint does not end at step 13"
    return ""


def references(resumed: Resumed, parameters: Mapping[str, Any]) -> dict[str, Any]:
    """What the decision and the investigation's result rest on: identifiers, never evidence."""
    state = resumed.state or {}
    result = state.get("result") or {}
    drafted = result.get("work_order") or {}
    cited = sorted({str(r) for r in result.get("evidence_references", [])})
    return {
        "decision": parameters["decision"],
        "approval_id": parameters["approval_id"],
        "work_order_id": parameters["work_order_id"],
        "proposal_version": parameters["proposal_version"],
        "decided_by": parameters["decided_by"],
        "resumed_run_id": resumed.run_id,
        "snapshot_id": resumed.snapshot_id,
        "policy_result_ids": [
            p["result_id"] for p in state.get("policy", []) if p.get("result_id")
        ],
        "evidence_references": cited,
        # Whether the decided work order is the one the investigation drafted, or a person's.
        "drafted_by_investigation": isinstance(drafted, Mapping)
        and drafted.get("work_order_id") == parameters["work_order_id"],
    }


class State(TypedDict):
    case_id: str
    snapshot_id: str
    completed: list[str]
    references: dict[str, Any]
    concluded_at: str


def pins_of_decision() -> dict[str, Any]:
    shape = "|".join(STEPS)
    return {
        "graph": f"decision@{GRAPH_VERSION}",
        "graph_shape": hashlib.sha256(shape.encode()).hexdigest(),
        "prompts": {}, "tools": {}, "skills": {},
    }  # fmt: skip


def build(context: Context, parameters: Mapping[str, Any]) -> StateGraph[State]:
    graph: StateGraph[State] = StateGraph(State)
    found: dict[str, Resumed | None] = {}

    async def resumed() -> Resumed | None:
        if "run" not in found:
            found["run"] = await context.resumed_run(str(parameters["resumes"]))
        return found["run"]

    async def receive_decision(state: State) -> dict[str, Any]:
        if not state["case_id"] or not state["snapshot_id"]:
            raise StepFailedError("A decision resumes a case on its snapshot")
        return {}

    async def check_compatibility(state: State) -> dict[str, Any]:
        why = incompatibility(await resumed(), case_id=state["case_id"],
                              snapshot_id=state["snapshot_id"],
                              policy_hash=context.policy_hash)  # fmt: skip
        if why:
            raise IncompatibleResumeError(f"{why}: restart the case from its snapshot")
        return {}

    async def restore(state: State) -> dict[str, Any]:
        run = await resumed()
        if run is None:  # Checked a step earlier; gone since means it cannot be carried on.
            raise IncompatibleResumeError("The run it resumes is gone: restart from the snapshot")
        return {"references": references(run, parameters)}

    async def record_references(state: State) -> dict[str, Any]:
        await context.save_result(state["references"])
        return {}

    async def conclude(state: State) -> dict[str, Any]:
        return {"concluded_at": datetime.now(UTC).isoformat()}

    own = {
        "receive_decision": receive_decision, "check_compatibility": check_compatibility,
        "restore": restore, "record_references": record_references, "conclude": conclude,
    }  # fmt: skip
    previous = START
    for position, name in enumerate(STEPS, start=1):

        def node(position: int = position, name: str = name) -> Any:
            async def run(state: State) -> dict[str, Any]:
                started = datetime.now(UTC)
                update = await own[name](state)
                await context.record(position, name, started)
                return {"completed": [*state["completed"], name], **update}

            return run

        graph.add_node(name, node())
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    return graph


@dataclass(frozen=True)
class Decision:
    name: str = "decision"
    version: str = GRAPH_VERSION

    def check(self, parameters: Mapping[str, str]) -> dict[str, Any]:
        wanted = {"resumes", "approval_id", "decision", "work_order_id", "proposal_version",
                  "decided_by"}  # fmt: skip
        unknown, missing = set(parameters) - wanted, wanted - set(parameters)
        if unknown:
            raise InvalidRequestError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        if missing:
            raise InvalidRequestError(f"Missing parameters: {', '.join(sorted(missing))}")
        checks = (
            (_RUN.fullmatch(parameters["resumes"]), "resumes must be a run's ID"),
            (_APPROVAL.fullmatch(parameters["approval_id"]), "approval_id must be an approval's"),
            (parameters["decision"] in DECISIONS, "decision must be approved or rejected"),
            (_WORK_ORDER.fullmatch(parameters["work_order_id"]),
             "work_order_id must be a work order's"),
            (parameters["proposal_version"].isdigit(), "proposal_version must be a number"),
            (_PERSON.fullmatch(parameters["decided_by"]), "decided_by must name a person"),
        )  # fmt: skip
        for passed, why in checks:
            if not passed:
                raise InvalidRequestError(why)
        return dict(parameters)

    def total_steps(self, parameters: Mapping[str, Any]) -> int:
        return len(STEPS)

    def pins(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        return pins_of_decision()

    async def run(self, context: Context, parameters: Mapping[str, Any]) -> None:
        checkpointer = context.checkpointer
        if checkpointer is None:
            raise StepFailedError("The decision workflow needs a checkpointer")
        graph = build(context, parameters).compile(checkpointer=checkpointer)
        config: RunnableConfig = {
            "configurable": {"thread_id": context.run_id},
            "run_name": "decision",
            "metadata": {
                **context.trace_metadata,
                "graph": f"decision@{GRAPH_VERSION}",
                "resumes": parameters["resumes"],
            },
        }
        started = await checkpointer.aget_tuple(config)
        inputs: State | None = None if started is not None else State(
            case_id=context.case_id, snapshot_id=context.snapshot_id, completed=[],
            references={}, concluded_at="",
        )  # fmt: skip
        final = await graph.ainvoke(inputs, config, durability="sync")
        if final.get("completed", [])[-1:] != ["conclude"]:
            raise StepFailedError("The decision ended before its last step")
