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

**What each step does now (M8.7, ADR 0021):** step 5 runs the team. Steps 4 to 9 happen inside
it: the Supervisor plans, delegates to its specialists, and synthesizes their findings. Steps 6
to 8 and 11 then record, from the tools the team called, whether each happened, and name a data
gap where it did not. Validating the report's structure (step 10) comes in M8.8, and the
recommendation and draft (step 12) in M8.8 and Phase 9. A run started without the team records a
gap for each, and, for checks, step 4 can ask the Supervisor for a one-line plan: a real call to
Claude, to prove a resumed run never makes it twice. Investigation starts runs, and step 13 sets
the case to pending_review, from M8.11.
"""

import asyncio
import hashlib
import json
import operator
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from windops_claude import stable_system
from windops_grpc import InvalidRequestError

from windops_agent_runtime.team import rehearsal
from windops_agent_runtime.team.definitions import team
from windops_agent_runtime.team.result import Recommendation, Seen
from windops_agent_runtime.workflows import ClaudeUnavailableError, Context, StepFailedError

GRAPH_VERSION = "3"
_TURBINE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
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
    # The Supervisor's report, whom it asked, and every tool its team called (M8.7).
    report: str
    delegated: Annotated[list[str], operator.add]
    tools_used: Annotated[list[str], operator.add]
    # What the team's tools returned that a claim may cite, the checked result, and the Policy
    # Service's outcome for each recommended action (M8.8).
    seen: dict[str, Any]
    result: dict[str, Any]
    policy: list[dict[str, str]]


def pins_of(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """What a run is pinned to (AGT-004). Prompts, tools and skills join as their modules land."""
    shape = "|".join(name for name, _ in STEPS)
    prompts: dict[str, str] = {}
    tools: dict[str, str] = {}
    skills: dict[str, str] = {}
    if parameters.get("plan_with_claude"):
        prompts["plan"] = hashlib.sha256(_PLANNER.encode()).hexdigest()
    if parameters.get("team"):
        pinned = team().pins()
        prompts.update(pinned["prompts"])
        tools, skills = pinned["tools"], pinned["skills"]
    return {
        "graph": f"investigation@{GRAPH_VERSION}",
        "graph_shape": hashlib.sha256(shape.encode()).hexdigest(),
        "prompts": prompts,
        "tools": tools,
        "skills": skills,
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
        if parameters.get("team"):
            # The Supervisor plans at the start of its own run, in step 5.
            return {}
        if not parameters.get("plan_with_claude"):
            return {"data_gaps": ["plan: the run was started without the agents"]}
        claude = context.claude
        if claude is None:
            raise ClaudeUnavailableError("The run asked for a plan, and the runtime has no key")
        result = await claude.invoke(
            "supervisor",
            [stable_system(_PLANNER), HumanMessage(f"Case {state['case_id']}: a synthetic check.")],
        )
        return {"plan": result.message.text.strip()[:500]}

    async def specialists(state: State) -> dict[str, Any]:
        if not parameters.get("team"):
            return {"data_gaps": ["specialists: the run was started without the agents"]}
        runner = context.team
        if runner is None:
            raise ClaudeUnavailableError(
                "The run needs its agents, and the runtime cannot run them"
            )
        # Steps 4 to 9 happen inside: the Supervisor plans, delegates and synthesizes.
        scripted = None
        if parameters.get("rehearse"):
            scripted = rehearsal.models(
                state["case_id"], state["snapshot_id"], str(parameters["turbine_id"]),
                sandbox=bool(parameters.get("sandbox")),
            )  # fmt: skip
        found = await runner.investigate(state["case_id"], state["snapshot_id"], model=scripted)
        return {"report": found.report, "delegated": list(found.delegated),
                "tools_used": list(found.tools_used), "seen": found.seen}  # fmt: skip

    def used(state: State, *names: str) -> bool:
        return any(tool.startswith(names) for tool in state["tools_used"])

    def covered(step: str, gap: str, *tools: str) -> Node:
        """Steps 6 to 8 and 11 happen inside the team's run: say whether they did."""

        async def check_used(state: State) -> dict[str, Any]:
            if not parameters.get("team"):
                return {"data_gaps": [f"{step}: arrives with the agents"]}
            return {} if used(state, *tools) else {"data_gaps": [f"{step}: {gap}"]}

        return check_used

    async def aggregate(state: State) -> dict[str, Any]:
        if parameters.get("team") and state["report"]:
            return {}
        return {"data_gaps": ["aggregate: there is no report from the Supervisor"]}

    async def validate_output(state: State) -> dict[str, Any]:
        """Step 10: the report, structured and checked, with at most two repairs (M8.8)."""
        if not parameters.get("team") or context.team is None:
            return {"data_gaps": ["validate_output: there is no report to structure"]}
        composed = await context.team.compose(
            state["case_id"], state["snapshot_id"], state["report"],
            Seen.from_dict(state["seen"]), rehearse=bool(parameters.get("rehearse")),
            turbine_id=str(parameters.get("turbine_id", "")),
        )  # fmt: skip
        return {"result": {**composed.draft.model_dump(mode="json"), "repairs": composed.repairs}}

    async def policy_checks(state: State) -> dict[str, Any]:
        """Step 11: every recommended action, through the Policy Service's deterministic rules."""
        if not state["result"] or context.team is None:
            return {"data_gaps": ["policy_checks: there is no result to check"]}
        actions = state["result"]["recommended_actions"]
        if not actions:
            return {"policy": [], "data_gaps": ["policy_checks: no action was recommended"]}
        checked = []
        for action in actions:
            outcome = await context.team.check_policy(
                state["case_id"], Recommendation.model_validate(action)
            )
            checked.append({"action": action["action"], "result_id": outcome.result_id,
                            "outcome": outcome.outcome})  # fmt: skip
        return {"policy": checked}

    async def recommend(state: State) -> dict[str, Any]:
        """Step 12: the result, completed and kept. Drafting a work order comes in Phase 9."""
        if not state["result"]:
            return {"data_gaps": ["recommend: there is no result to recommend from"]}
        policy = state["policy"]
        result = {
            **state["result"],
            "policy": policy,
            "policy_result_id": policy[0]["result_id"] if policy else "",
            # Blocked or escalated by policy means a person must look (FR-POL-005).
            "escalation_required": state["result"]["escalation_required"]
            or any(p["outcome"] != "pass" for p in policy),
            "model_versions": await context.models_used(),
            "generated_at": datetime.now(UTC).isoformat(),
            "run_data_gaps": state["data_gaps"],
        }
        await context.save_result(result)
        return {"result": result, "data_gaps": ["recommend: draft work orders come in Phase 9"]}

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
        "specialists": specialists,
        "documents": covered("documents", "no specialist searched the approved documents",
                             "documentation_"),
        "research": covered("research", "no specialist read the forecast or researched",
                            "weather_", "tavily_"),
        "impact": covered("impact", "no estimate was asked for", "windops_estimate_impact"),
        "aggregate": aggregate,
        "validate_output": validate_output,
        "policy_checks": policy_checks,
        "recommend": recommend,
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
        unknown = set(parameters) - {
            "step_seconds", "plan_with_claude", "team", "rehearse", "turbine_id", "sandbox",
        }  # fmt: skip
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
        agents = parameters.get("team", "true")
        if agents not in ("true", "false"):
            raise InvalidRequestError("team must be true or false")
        # A rehearsal runs the real team on a scripted model: no call reaches Claude (M8.7).
        rehearse = parameters.get("rehearse", "false")
        if rehearse not in ("true", "false"):
            raise InvalidRequestError("rehearse must be true or false")
        turbine = parameters.get("turbine_id", "")
        if rehearse == "true" and not (agents == "true" and _TURBINE.fullmatch(turbine)):
            raise InvalidRequestError("A rehearsal needs the team and a turbine_id")
        # A rehearsal may also run a real Sandbox task, which costs a little LangSmith compute.
        sandbox = parameters.get("sandbox", "false")
        if sandbox not in ("true", "false") or (sandbox == "true" and rehearse != "true"):
            raise InvalidRequestError("sandbox must be true or false, and only for a rehearsal")
        return {"step_seconds": seconds, "plan_with_claude": plan == "true",
                "team": agents == "true", "rehearse": rehearse == "true",
                "turbine_id": turbine, "sandbox": sandbox == "true"}  # fmt: skip

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
            # The trace's root: the run's IDs, and its correlation ID (OBS-005, OBS-008).
            "run_name": "investigation",
            "metadata": {**context.trace_metadata, "graph": f"investigation@{GRAPH_VERSION}"},
        }
        started = await checkpointer.aget_tuple(config)
        # A new run starts from its inputs. A run claimed again carries on from its checkpoint.
        inputs: State | None = None if started is not None else State(
            case_id=context.case_id, snapshot_id=context.snapshot_id, completed=[], data_gaps=[],
            plan="", concluded_at="", report="", delegated=[], tools_used=[], seen={}, result={},
            policy=[],
        )  # fmt: skip
        # "sync": each checkpoint is written before the next node starts, so a worker that stops
        # loses at most the node it was in.
        final = await graph.ainvoke(inputs, config, durability="sync")
        if final.get("completed", [])[-1:] != ["conclude"]:
            raise StepFailedError("The graph ended before step 13")


def canonical_pins(pins: Mapping[str, Any]) -> str:
    return json.dumps(pins, sort_keys=True, separators=(",", ":"))
