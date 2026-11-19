"""Runs the team on one case, and reports what it found and how (M8.7).

The Supervisor gets the case's ID and evidence snapshot, after its cached prompt (LLM-007), and
works from there: it reads the case, plans, delegates to its specialists through Deep Agents'
task tool, and answers with its report. The report is text until M8.8 gives it its structure.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from windops_claude import Claude

from windops_agent_runtime.team.build import assemble
from windops_agent_runtime.team.definitions import Team
from windops_agent_runtime.team.tools import Tokens, ToolBox

# Model and tool steps the Supervisor may take. Its budget usually ends a run long before.
RECURSION_LIMIT = 80


@dataclass(frozen=True)
class Findings:
    report: str
    delegated: tuple[str, ...]
    tools_used: tuple[str, ...]


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
    ) -> None:
        self._team, self._toolbox, self._claude = team, toolbox, claude
        self._tokens, self._tool_calls, self._model = tokens, tool_calls, model

    async def investigate(
        self, case_id: str, snapshot_id: str, *, model: Callable[[str], Any] | None = None
    ) -> Findings:
        """model replaces every agent's model, as a rehearsal does. Otherwise each is Claude."""
        assembled = await assemble(
            self._team, claude=self._claude, tokens=self._tokens, toolbox=self._toolbox,
            tool_calls=self._tool_calls, model=model or self._model,
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
            tools_used=tuple(assembled.usage.calls),
        )  # fmt: skip
