"""The investigation team as Deep Agents: the Supervisor, delegating to five specialists (SRS 5.3).

Deep Agents is restricted as ADR 0015 found it must be, through a harness profile for this
platform's models:

- **No default model:** every agent's model is a MeteredChat for its own role (LLM-001).
- **No summarization:** the middleware that rewrites history is excluded (LLM-008).
- **No general-purpose subagent:** the Supervisor delegates only to the five named specialists.
- **No file or shell tools:** writing, editing, deleting, searching and executing are excluded.
  What is left, listing and reading, reaches only the run's own skills, held in its state, and
  never the host filesystem. A permission rule denies everything else.

Each agent gets exactly its definition's tools (ADR 0020), and its own skills, with one exception:
in the investigation the Maintenance Planner advises without its drafting tool, which it gets in
step 12, once the result has passed its checks and policy (M9.3). Every tool call by every agent
counts against the run's tool-call budget (SRS 5.6).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfileConfig,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.utils import create_file_data
from langchain_core.tools import BaseTool
from windops_claude import BudgetExhaustedError, Claude

from windops_agent_runtime.team.definitions import SUPERVISOR, Team
from windops_agent_runtime.team.metered import PROVIDER, MeteredChat
from windops_agent_runtime.team.result import Seen
from windops_agent_runtime.team.sandbox import SandboxRun
from windops_agent_runtime.team.tools import Tokens, ToolBox

# Held back from the team in step 5: a work order is drafted only in step 12 (M9.3).
DRAFTING_TOOLS = frozenset({"mcp.windops.create_draft_work_order"})
EXCLUDED_TOOLS = frozenset(
    {"write_file", "edit_file", "delete", "execute", "write_todos", "glob", "grep"}
)
PERMISSIONS = [
    FilesystemPermission(operations=["read"], paths=["/skills/**"]),
    FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="deny"),
]
_registered: list[bool] = []


def register_profile() -> None:
    """Once per process: the harness profile every agent built on MeteredChat gets."""
    if _registered:
        return
    register_harness_profile(
        PROVIDER,
        HarnessProfileConfig(
            excluded_middleware=frozenset({"SummarizationMiddleware"}),
            excluded_tools=EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _registered.append(True)


@dataclass
class Usage:
    """Every agent's tool calls in this run, counted against its budget."""

    limit: int
    calls: list[str] = field(default_factory=list)
    # What the tools returned that a claim may cite, and the estimates (M8.8).
    seen: Seen = field(default_factory=Seen)

    def count(self, name: str) -> None:
        if len(self.calls) >= self.limit:
            raise BudgetExhaustedError(f"Used all {self.limit} of its tool calls")
        self.calls.append(name)


def chat_for(
    team: Team, claude: Claude, role: str, model: Callable[[str], Any] | None = None
) -> Any:
    """An agent's model: Claude for its own role, or a script's. Labelled for its trace runs."""
    agent = team.agents[role]
    configured = claude.policy.role(agent.role)
    built = model(role) if model is not None else MeteredChat(role=agent.role, claude=claude)
    # On each model run in the trace: who, on what, and from which prompt (OBS-006). A
    # rehearsal's scripted model says so.
    built.metadata = {
        "agent": agent.name,
        "model": configured.model if model is None else "scripted",
        "effort": configured.effort,
        "prompt_version": f"{agent.version}:{agent.prompt_hash[:12]}",
    }
    return built


@dataclass(frozen=True)
class Assembled:
    agent: Any
    files: dict[str, Any]
    usage: Usage


async def assemble(
    team: Team,
    *,
    claude: Claude,
    tokens: Tokens,
    toolbox: ToolBox,
    tool_calls: int,
    model: Callable[[str], Any] | None = None,
    sandbox_run: SandboxRun | None = None,
) -> Assembled:
    """The Supervisor, with its specialists, for one run."""
    register_profile()
    usage = Usage(limit=tool_calls)

    def chat(role: str) -> Any:
        return chat_for(team, claude, role, model)

    async def tools_of(name: str) -> list[BaseTool]:
        agent = team.agents[name]
        scopes = tuple(s for s in agent.tools if s not in DRAFTING_TOOLS)
        return await toolbox.tools_for(
            name, scopes, tokens, before_call=usage.count, after_call=usage.seen.add,
            sandbox_run=sandbox_run,
        )  # fmt: skip

    supervisor = team.agents[SUPERVISOR]
    files: dict[str, Any] = {}
    subagents: list[Any] = []
    for name in supervisor.delegates:
        agent = team.agents[name]
        # Each specialist sees only its own skills, under its own folder.
        for skill in agent.skills:
            files[f"/skills/{name}/{skill}/SKILL.md"] = create_file_data(team.skills[skill].content)
        subagents.append(
            {
                "name": name,
                "description": agent.description,
                "system_prompt": agent.prompt,
                "tools": await tools_of(name),
                "model": chat(agent.role),
                "skills": [f"/skills/{name}/"] if agent.skills else [],
                "permissions": PERMISSIONS,
            }
        )
    built = create_deep_agent(
        model=chat(supervisor.role),
        tools=await tools_of(SUPERVISOR),
        system_prompt=supervisor.prompt,
        subagents=subagents,
        permissions=PERMISSIONS,
        name=SUPERVISOR,
    )
    return Assembled(agent=built, files=files, usage=usage)
