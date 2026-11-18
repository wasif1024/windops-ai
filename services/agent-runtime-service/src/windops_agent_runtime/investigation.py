"""The investigation workflow: SRS 5.4's steps 1 to 13, as a LangGraph graph (M8.4, ADR 0018).

    receive -> validate -> snapshot -> plan -> specialists -> documents -> research -> impact
      -> aggregate -> validate_output -> policy_checks -> recommend -> conclude

**Checkpoints:** LangGraph saves one after every node, through `RunCheckpointer`, under the run's
lease. A worker that stops loses at most the node it was in. The next claim carries on from the
newest checkpoint, and LangGraph skips every node it has passed.

**Human decisions:** the graph never waits on one. There is no `interrupt()` and no interrupt
point, because a LangGraph interrupt is not the system of record for a decision (AGT-003). The
graph ends at step 13. The decision arrives later as a durable event, and starts a run of its
own (step 14, Phase 10).

**Pins:** a run pins the graph's version and shape, and the prompt, tool and skill versions it
uses (AGT-004). A resume under different pins ends the run as stale (AGT-005).

**What each step does now:** only steps 1, 2, 3 and 13 have work to do, and none of them changes a
case yet. Steps 4 to 12 come with the agents, their tools and structured results (M8.6 to M8.8,
Phase 9). Until then each records a data gap naming its module, so a run's result says plainly
what was not done. Step 4 can, for checks, ask the Supervisor for a one-line plan, a real call to
Claude, to prove that a resumed run never makes the same call twice. Investigation starts runs,
and step 13 sets the case to pending_review, once the steps have content (M8.11).
"""

import asyncio
import hashlib
import json
import operator
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from windops_claude import stable_system
from windops_grpc import InvalidRequestError

from windops_agent_runtime.workflows import ClaudeUnavailableError, Context, StepFailedError

GRAPH_VERSION = "1"
# SRS 5.4 steps 1 to 13, in order, and the module that gives each its content.
STEPS: tuple[tuple[str, str], ...] = (
    ("receive", "M8.4"),
    ("validate", "M8.5"),
    ("snapshot", "M8.4"),
    ("plan", "M8.7"),
    ("specialists", "M8.6, M8.7"),
    ("documents", "M8.6"),
    ("research", "M8.6"),
    ("impact", "M8.6"),
    ("aggregate", "M8.7"),
    ("validate_output", "M8.8"),
    ("policy_checks", "M8.6"),
    ("recommend", "M8.7, M9.3"),
    ("conclude", "M8.11"),
)
_PLANNER = (
    "You are the Supervisor of a wind turbine fault investigation. Given a case, reply with one "
    "short sentence naming the specialists you would ask first, and why."
)


class State(TypedDict):
    case_id: str
    snapshot_id: str
    # Appended to, never rewritten, as each node adds its own.
    completed: Annotated[list[str], operator.add]
    data_gaps: Annotated[list[str], operator.add]
    plan: str
    concluded_at: str


def pins_of(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """What a run is pinned to (AGT-004). Prompts, tools and skills join as their modules land."""
    shape = "|".join(name for name, _ in STEPS)
    return {
        "graph": f"investigation@{GRAPH_VERSION}",
        "graph_shape": hashlib.sha256(shape.encode()).hexdigest(),
        "prompts": {"plan": hashlib.sha256(_PLANNER.encode()).hexdigest()}
        if parameters.get("plan_with_claude")
        else {},
        "tools": {},
        "skills": {},
    }


Node = Callable[[State], Awaitable[dict[str, Any]]]


def build(context: Context, parameters: Mapping[str, Any]) -> StateGraph[State]:
    """The graph, its nodes bound to this run's context and parameters."""
    seconds = float(parameters["step_seconds"])
    graph: StateGraph[State] = StateGraph(State)

    def node(position: int, name: str, work: Node) -> Node:
        async def run(state: State) -> dict[str, Any]:
            started = datetime.now(UTC)
            if seconds:
                await asyncio.sleep(seconds)
            update = await work(state)
            await context.record(position, name, started)
            return {"completed": [name], **update}

        return run

    async def receive(state: State) -> dict[str, Any]:
        if not state["case_id"] or not state["snapshot_id"]:
            raise StepFailedError("A run needs its case and evidence snapshot")
        return {}

    async def snapshot(state: State) -> dict[str, Any]:
        # The snapshot was frozen by Investigation before the run was queued (EVD-001), and the
        # run is pinned to it. Reading it through WindOps MCP comes in M8.6.
        return {"data_gaps": ["snapshot: read through WindOps MCP from M8.6"]}

    async def plan(state: State) -> dict[str, Any]:
        if not parameters.get("plan_with_claude"):
            return {"data_gaps": ["plan: the Supervisor plans from M8.7"]}
        claude = context.claude
        if claude is None:
            raise ClaudeUnavailableError("The run asked for a plan, and the runtime has no key")
        result = await claude.invoke(
            "supervisor",
            [stable_system(_PLANNER), HumanMessage(f"Case {state['case_id']}: a synthetic check.")],
        )
        return {"plan": result.message.text.strip()[:500]}

    async def conclude(state: State) -> dict[str, Any]:
        # Setting the case to pending_review comes with Investigation's side of the run (M8.11).
        return {"concluded_at": datetime.now(UTC).isoformat(), "data_gaps": [
            "conclude: the case is set to pending_review from M8.11"]}  # fmt: skip

    def deferred(name: str, module: str) -> Node:
        async def gap(state: State) -> dict[str, Any]:
            return {"data_gaps": [f"{name}: arrives in {module}"]}

        return gap

    async def nothing(state: State) -> dict[str, Any]:
        return {}

    own: dict[str, Node] = {
        "receive": receive, "validate": nothing, "snapshot": snapshot, "plan": plan,
        "conclude": conclude,
    }  # fmt: skip
    previous = START
    for position, (name, module) in enumerate(STEPS, start=1):
        graph.add_node(name, node(position, name, own.get(name) or deferred(name, module)))  # type: ignore[call-overload]
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    return graph


@dataclass(frozen=True)
class Investigation:
    name: str = "investigation"
    version: str = GRAPH_VERSION

    def check(self, parameters: Mapping[str, str]) -> dict[str, Any]:
        unknown = set(parameters) - {"step_seconds", "plan_with_claude"}
        if unknown:
            raise InvalidRequestError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        try:
            seconds = float(parameters.get("step_seconds", "0"))
        except ValueError as error:
            raise InvalidRequestError("step_seconds must be a number") from error
        if not 0 <= seconds <= 30:
            raise InvalidRequestError("step_seconds must be from 0 to 30")
        plan = parameters.get("plan_with_claude", "false")
        if plan not in ("true", "false"):
            raise InvalidRequestError("plan_with_claude must be true or false")
        return {"step_seconds": seconds, "plan_with_claude": plan == "true"}

    def total_steps(self, parameters: Mapping[str, Any]) -> int:
        return len(STEPS)

    def pins(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        return pins_of(parameters)

    async def run(self, context: Context, parameters: Mapping[str, Any]) -> None:
        checkpointer = context.checkpointer
        if checkpointer is None:
            raise StepFailedError("The investigation workflow needs a checkpointer")
        graph = build(context, parameters).compile(checkpointer=checkpointer)
        config: RunnableConfig = {
            "configurable": {"thread_id": context.run_id},
            "recursion_limit": 50,
        }
        started = await checkpointer.aget_tuple(config)
        # A new run starts from its inputs. A run claimed again carries on from its checkpoint.
        inputs: State | None = None if started is not None else State(
            case_id=context.case_id, snapshot_id=context.snapshot_id, completed=[], data_gaps=[],
            plan="", concluded_at="",
        )  # fmt: skip
        # "sync": each checkpoint is written before the next node starts, so a worker that stops
        # loses at most the node it was in.
        final = await graph.ainvoke(inputs, config, durability="sync")
        if final.get("completed", [])[-1:] != ["conclude"]:
            raise StepFailedError("The graph ended before step 13")


def canonical_pins(pins: Mapping[str, Any]) -> str:
    return json.dumps(pins, sort_keys=True, separators=(",", ":"))
