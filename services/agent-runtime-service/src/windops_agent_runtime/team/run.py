"""Runs the team on one case, and reports what it found and how (M8.7).

The Supervisor gets the case's ID and evidence snapshot, after its cached prompt (LLM-007), and
works from there: it reads the case, plans, delegates to its specialists through Deep Agents'
task tool, and answers with its report. The report is text until M8.8 gives it its structure.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from windops_claude import Claude

from windops_agent_runtime.team import rehearsal
from windops_agent_runtime.team.build import assemble
from windops_agent_runtime.team.compose import Composed, InvalidResultError, compose
from windops_agent_runtime.team.definitions import SUPERVISOR, Team
from windops_agent_runtime.team.result import Recommendation, Seen, problems
from windops_agent_runtime.team.sandbox import SandboxRun
from windops_agent_runtime.team.tools import Tokens, ToolBox

# Model and tool steps the Supervisor may take. Its budget usually ends a run long before.
RECURSION_LIMIT = 80


@dataclass(frozen=True)
class Findings:
    report: str
    delegated: tuple[str, ...]
    tools_used: tuple[str, ...]
    # What the tools returned that the result may cite (M8.8).
    seen: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyCheck:
    result_id: str
    outcome: str


class TeamRunner:
    def __init__(
        self,
        team: Team,
        toolbox: ToolBox,
        *,
        claude: Claude,
        tokens: Tokens,
        tool_calls: int,
        model: Callable[[str], Any] | None = None,
        sandbox_run: SandboxRun | None = None,
    ) -> None:
        self._team, self._toolbox, self._claude = team, toolbox, claude
        self._tokens, self._tool_calls, self._model = tokens, tool_calls, model
        self._sandbox_run = sandbox_run

    async def investigate(
        self, case_id: str, snapshot_id: str, *, model: Callable[[str], Any] | None = None
    ) -> Findings:
        """model replaces every agent's model, as a rehearsal does. Otherwise each is Claude."""
        assembled = await assemble(
            self._team, claude=self._claude, tokens=self._tokens, toolbox=self._toolbox,
            tool_calls=self._tool_calls, model=model or self._model, sandbox_run=self._sandbox_run,
        )  # fmt: skip
        question = (
            f"Investigate case {case_id}. Its frozen evidence is snapshot {snapshot_id}. Read "
            "the case and its evidence, plan, delegate to the specialists you need, and report."
        )
        out = await assembled.agent.ainvoke(
            {"messages": [HumanMessage(question)], "files": assembled.files},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        messages = out["messages"]
        delegated = tuple(
            str(call["args"].get("subagent_type", ""))
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
            if call["name"] == "task"
        )
        return Findings(
            report=messages[-1].text.strip(), delegated=delegated,
            tools_used=tuple(assembled.usage.calls), seen=assembled.usage.seen.as_dict(),
        )  # fmt: skip

    async def compose(
        self, case_id: str, snapshot_id: str, report: str, seen: Seen, *, rehearse: bool = False,
        turbine_id: str = "",
    ) -> Composed:  # fmt: skip
        """Step 10: the report as a result that passes its checks. A rehearsal drafts it
        without Claude, and still checks it."""
        if rehearse:
            draft = rehearsal.draft(case_id, snapshot_id, turbine_id, seen)
            found = problems(draft, case_id=case_id, snapshot_id=snapshot_id, seen=seen)
            if found:
                raise InvalidResultError("; ".join(found))
            return Composed(draft=draft, repairs=0)
        prompt = self._team.agents[SUPERVISOR].result_prompt
        return await compose(
            self._claude, prompt, case_id=case_id, snapshot_id=snapshot_id, report=report,
            seen=seen,
        )  # fmt: skip

    async def check_policy(self, case_id: str, action: Recommendation) -> PolicyCheck:
        """Step 11: the Policy Service's deterministic rules on one recommended action, asked as
        the Supervisor, with its own token, through WindOps MCP."""
        answer = await self._toolbox.call(
            SUPERVISOR, "mcp.windops.check_policy", self._tokens,
            {"case_id": case_id, "action": action.action, "summary": action.summary,
             "requires_climb": action.requires_climb,
             "impact_result_id": action.impact_result_id},
        )  # fmt: skip
        found = json.loads(answer)
        if "error" in found or not found.get("result_id"):
            raise PolicyUnavailableError(str(found.get("error", "no result")))
        return PolicyCheck(result_id=str(found["result_id"]), outcome=str(found["outcome"]))


class PolicyUnavailableError(Exception):
    """The Policy Service gave no result. The run cannot recommend without one (SRS 7.7)."""
