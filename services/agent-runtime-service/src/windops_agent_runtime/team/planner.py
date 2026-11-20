"""Step 12's draft: the Maintenance Planner prepares a proposal and drafts its work order (M9.3).

Once the result has passed its checks and the Policy Service's rules, the planner is run on its
own, as the main agent, from its drafting prompt (draft.md). It gets:

- the action to plan: the first recommended action that policy did not block, or blocked only by
  rules judged over a planned window, which a recommendation does not have. The draft has one,
  and the Maintenance Service asks the Policy Service again over it: a block then stops its
  submission, as any block does;
- the investigation's cited facts, hypotheses, data gaps and risk flags;
- the approved procedures the run read, the only ones it may cite;
- the time now, after its cached prompt (LLM-007).

It reads the turbine's history, finds and checks parts, and drafts one work order through WindOps
MCP (FR-MNT-001). A second draft is refused here, before any call. Its tool calls count against
what is left of the run's budget. What it may do is still its principal's: a Maintenance Service
that refuses the draft, as it does for someone who may not plan, leaves the run with no draft and a
data gap, never a failure. The draft is only a draft: a person submits it, and another decides.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from windops_claude import Claude

from windops_agent_runtime.team.build import PERMISSIONS, Usage, chat_for, register_profile
from windops_agent_runtime.team.definitions import PLANNER, Team
from windops_agent_runtime.team.tools import Tokens, ToolBox

DRAFT_TOOL = "windops_create_draft_work_order"
# Model and tool steps the planner may take: a few reads, the draft, and its answer.
RECURSION_LIMIT = 40
_PROPOSAL_LIMIT = 8000


def choose(
    actions: Sequence[Mapping[str, Any]], policy: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """The action to draft: the first one the Policy Service did not block, or blocked only for
    want of a planned window. None if none."""
    for action, outcome in zip(actions, policy, strict=False):
        if outcome.get("outcome") in ("pass", "escalate") or (
            outcome.get("outcome") == "block" and outcome.get("window_only") is True
        ):
            return action, outcome
    return None


def procedures_of(references: Sequence[str]) -> list[str]:
    """The approved passages the run read, by citation: the only procedures a draft may cite."""
    return sorted(r for r in references if r.startswith("kn:"))


def brief(
    *,
    case_id: str,
    snapshot_id: str,
    turbine_id: str,
    result: Mapping[str, Any],
    action: Mapping[str, Any],
    policy: Mapping[str, Any],
    procedures: Sequence[str],
    now: datetime,
) -> str:
    """The case's data, after the cached prompt."""
    findings = {
        key: result.get(key, [])
        for key in ("observed_facts", "hypotheses", "conflicting_evidence", "data_gaps",
                    "risk_flags")
    }  # fmt: skip
    return "\n\n".join(
        [
            f"Case: {case_id}\nTurbine: {turbine_id}\nSnapshot: {snapshot_id}\n"
            f"Time now: {now.isoformat()}",
            "The action to plan:\n" + json.dumps(dict(action), sort_keys=True),
            f"Its policy outcome: {policy.get('outcome', '')} ({policy.get('result_id', '')})"
            + (
                "\nBlocked only by weather rules judged over a planned window: your draft's "
                "window will be judged on its forecast."
                if policy.get("window_only")
                else ""
            ),
            "The investigation's findings:\n" + json.dumps(findings, sort_keys=True),
            "Approved procedures you may cite, exactly as written:\n"
            + ("\n".join(procedures) if procedures else "(none was read in this run)"),
        ]
    )


@dataclass
class Drafting:
    """What drafting found: the work order, if one was drafted, and why not otherwise."""

    work_order: dict[str, Any] | None = None
    refused: str = ""
    proposal: str = ""
    tools_used: list[str] = field(default_factory=list)

    def note(self, tool: str, answer: str) -> None:
        if tool != DRAFT_TOOL:
            return
        try:
            found = json.loads(answer)
        except ValueError:
            found = {"error": answer[:500]}
        if isinstance(found, dict) and found.get("work_order_id"):
            self.work_order = {
                key: found.get(key)
                for key in ("work_order_id", "status", "revision", "drafted_by_agent", "passed",
                            "problems", "policy_outcome")
            }  # fmt: skip
        elif self.work_order is None:
            self.refused = str(found.get("error", "no work order") if isinstance(found, dict)
                               else found)[:500]  # fmt: skip


def _one_draft(tool: BaseTool, drafting: Drafting) -> BaseTool:
    """The drafting tool, refusing a second draft before it reaches WindOps MCP."""
    original = tool.coroutine  # type: ignore[attr-defined]

    async def call(**arguments: Any) -> str:
        if drafting.work_order is not None:
            drafted = drafting.work_order["work_order_id"]
            return json.dumps({"error": f"This run already drafted {drafted}: a person fixes it"})
        answer: str = await original(**arguments)
        return answer

    schema = tool.args_schema if tool.args_schema is not None else {}
    return StructuredTool(
        name=tool.name, description=tool.description, args_schema=schema, coroutine=call
    )


async def draft(
    team: Team,
    *,
    claude: Claude,
    tokens: Tokens,
    toolbox: ToolBox,
    tool_calls: int,
    briefing: str,
    model: Callable[[str], Any] | None = None,
) -> Drafting:
    """Run the planner on the brief, with its own tools and token, and say what it drafted."""
    register_profile()
    drafting = Drafting()
    usage = Usage(limit=tool_calls)
    planner = team.agents[PLANNER]

    def counted(name: str) -> None:
        usage.count(name)
        drafting.tools_used.append(name)

    tools = await toolbox.tools_for(
        PLANNER, planner.tools, tokens, before_call=counted, after_call=drafting.note
    )
    tools = [_one_draft(t, drafting) if t.name == DRAFT_TOOL else t for t in tools]
    files = {
        f"/skills/{PLANNER}/{skill}/SKILL.md": create_file_data(team.skills[skill].content)
        for skill in planner.skills
    }
    built: Any = create_deep_agent(
        model=chat_for(team, claude, PLANNER, model),
        tools=tools,
        system_prompt=planner.draft_prompt,
        subagents=[],
        skills=[f"/skills/{PLANNER}/"] if planner.skills else None,
        permissions=PERMISSIONS,
        name=PLANNER,
    )
    out = await built.ainvoke(
        {"messages": [HumanMessage(briefing)], "files": files},
        config={"recursion_limit": RECURSION_LIMIT},
    )
    last = out["messages"][-1]
    drafting.proposal = (last.text if isinstance(last, AIMessage) else "").strip()[:_PROPOSAL_LIMIT]
    if drafting.work_order is None and not drafting.refused:
        drafting.refused = "The planner drafted no work order"
    return drafting
