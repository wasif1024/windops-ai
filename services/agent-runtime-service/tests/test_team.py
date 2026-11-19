"""The investigation team: its definitions, restrictions and a run, with scripted models (M8.7).

No call reaches Claude or an MCP server here. make team-check runs the team for real.
"""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from team_support import BOUND, Scripted, Tokens, call
from windops_agent_runtime.team.build import EXCLUDED_TOOLS
from windops_agent_runtime.team.definitions import SUPERVISOR, load_agent, load_team, team
from windops_agent_runtime.team.metered import MeteredChat
from windops_agent_runtime.team.run import TeamRunner
from windops_agent_runtime.team.tools import ToolBox, stand_in
from windops_claude import Budget, BudgetExhaustedError, Claude, MemoryLedger, Meter, load
from windops_claude.conversation import VolatilePromptError

POLICY = load()
TEAM = team()
CASE, SNAPSHOT = "case_" + "a" * 20, "evs_" + "b" * 20


async def turbine(arguments: dict[str, Any]) -> str:
    return '{"turbine_id": "dogger-east-t01", "model": "WS-8.0-167"}'


def runner(scripts: dict[str, list[AIMessage]], tokens: Tokens, tool_calls: int = 50) -> TeamRunner:
    every = {
        f"{scope.split('.')[1]}_{scope.split('.')[2]}": turbine
        for agent in TEAM.agents.values()
        for scope in agent.tools
    }
    box = ToolBox(stand_in(every))
    claude = Claude(POLICY, Meter(POLICY.budgets.default, MemoryLedger()), lambda: "key")
    return TeamRunner(
        TEAM, box, claude=claude, tokens=tokens, tool_calls=tool_calls,
        model=lambda role: Scripted(role=role, script=scripts.setdefault(role, [AIMessage("ok")])),
    )  # fmt: skip


# --- Definitions


def test_the_team_is_srs_5_3s_with_its_six_skills() -> None:
    assert set(TEAM.agents) == {
        "supervisor", "scada-analyst", "reliability-engineer", "production-impact",
        "maintenance-planner", "safety-researcher",
    }  # fmt: skip
    assert set(TEAM.skills) == {
        "scada-analysis", "gearbox-diagnostics", "vibration-analysis", "maintenance-planning",
        "weather-impact", "safety-procedures",
    }  # fmt: skip
    assert set(TEAM.agents[SUPERVISOR].delegates) == set(TEAM.agents) - {SUPERVISOR}
    # Each role's model and effort come from the Claude policy, never the definition.
    assert all(agent.role in POLICY.roles for agent in TEAM.agents.values())


def test_every_change_to_a_prompt_tool_list_or_skill_changes_the_pins() -> None:
    pins = TEAM.pins()
    assert set(pins["prompts"]) == {*TEAM.agents, "supervisor:result"}
    assert set(pins["skills"]) == set(TEAM.skills)
    assert pins["prompts"]["supervisor"].startswith("1:")


def test_a_prompt_that_would_not_cache_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "scada-analyst"
    folder.mkdir()
    (folder / "agent.yaml").write_text("name: scada-analyst\nversion: 1\nrole: scada-analyst\n"
                                       "description: d\n")  # fmt: skip
    (folder / "prompt.md").write_text("You are the SCADA Analyst. Today is 2026-11-19.")
    with pytest.raises(VolatilePromptError):
        load_agent(folder)


def test_a_role_outside_the_policy_or_a_missing_skill_is_refused(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    for folder in ("agents", "skills"):
        (tmp_path / folder).mkdir()
        for item in (root / folder).iterdir():
            (tmp_path / folder / item.name).symlink_to(item)
    rogue = tmp_path / "agents" / "rogue"
    rogue.mkdir()
    (rogue / "prompt.md").write_text("You are a rogue agent.")
    (rogue / "agent.yaml").write_text("name: rogue\nversion: 1\nrole: approver\ndescription: d\n")
    with pytest.raises(ValueError, match="not in the Claude policy"):
        load_team(tmp_path)
    (rogue / "agent.yaml").write_text(
        "name: rogue\nversion: 1\nrole: supervisor\ndescription: d\nskills: [hacking]\n"
    )
    with pytest.raises(ValueError, match="skills that do not exist"):
        load_team(tmp_path)


# --- Restrictions (ADR 0015) and a run


def test_a_run_delegates_and_its_specialist_calls_its_own_tool_with_its_own_token() -> None:
    tokens = Tokens()
    scripts = {
        "supervisor": [
            call("task", subagent_type="scada-analyst", description=f"Summarize {CASE}"),
            AIMessage("Report: gearbox oil temperature high, cited tel_1."),
        ],
        "scada-analyst": [call("windops_get_turbine", turbine_id="dogger-east-t01"),
                          AIMessage("SCADA: WS-8.0-167, readings normal.")],
    }  # fmt: skip
    found = asyncio.run(runner(scripts, tokens).investigate(CASE, SNAPSHOT))
    assert found.report.startswith("Report:")
    assert found.delegated == ("scada-analyst",)
    assert found.tools_used == ("windops_get_turbine",)
    # One token to read the tool's schema, one for the call: each for that agent and tool alone.
    assert set(tokens.asked) >= {
        ("scada-analyst", "windops-mcp-windops", ("mcp.windops.get_turbine",))
    }
    # The Supervisor delegates and reads its own tools; specialists never see the task tool.
    assert "task" in BOUND["supervisor"] and "task" not in BOUND["scada-analyst"]
    for role, names in BOUND.items():
        assert not set(names) & EXCLUDED_TOOLS, role
        assert set(names) - {"ls", "read_file", "task"} <= {
            f"{scope.split('.')[1]}_{scope.split('.')[2]}" for scope in TEAM.agents[role].tools
        }, role
    assert "general-purpose" not in str(BOUND)


def test_the_tool_call_budget_ends_the_run() -> None:
    scripts = {
        "supervisor": [call("task", subagent_type="scada-analyst", description="Look")],
        "scada-analyst": [call("windops_get_turbine", turbine_id="t1"), AIMessage("done")],
    }
    with pytest.raises(BudgetExhaustedError, match="tool calls"):
        asyncio.run(runner(scripts, Tokens(), tool_calls=0).investigate(CASE, SNAPSHOT))


# --- The metered model


class FakeChat:
    def __init__(self, answer: AIMessage) -> None:
        self.answer = answer
        self.options: dict[str, Any] = {}

    async def ainvoke(self, messages: list[Any], **options: Any) -> AIMessage:
        self.options = options
        return self.answer


def test_every_agent_call_goes_through_the_claude_policy() -> None:
    made: list[FakeChat] = []
    answer = AIMessage(content="ok", response_metadata={
        "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 10}})  # fmt: skip

    def make(policy: Any, role: str, *, api_key: str, max_tokens: int) -> FakeChat:
        made.append(FakeChat(answer))
        return made[-1]

    ledger = MemoryLedger()
    budget = Budget(tokens=1000, cost_usd=Decimal(1))
    claude = Claude(POLICY, Meter(budget, ledger), lambda: "key", make_model=make)  # type: ignore[arg-type]
    model = MeteredChat(role="scada-analyst", claude=claude)
    bound = model.bind_tools([{"name": "windops_get_turbine", "description": "d",
                               "input_schema": {"type": "object", "properties": {}}}])  # fmt: skip
    asyncio.run(bound.ainvoke([ToolMessage("x", tool_call_id="1")]))
    assert ledger.calls[0].role == "scada-analyst" and ledger.calls[0].effort == "medium"
    assert made[0].options["tools"][0]["name"] == "windops_get_turbine"
    with pytest.raises(ValueError, match="Forced tool choice"):
        model.bind_tools([], tool_choice="any")


def test_a_rehearsal_walks_the_real_team_through_every_agent_without_claude() -> None:
    from windops_agent_runtime.team import rehearsal

    tokens = Tokens()
    team_runner = runner({}, tokens)
    found = asyncio.run(team_runner.investigate(
        CASE, SNAPSHOT, model=rehearsal.models(CASE, SNAPSHOT, "dogger-east-t01")
    ))  # fmt: skip
    assert found.report == rehearsal.REPORT
    assert set(found.delegated) == set(TEAM.agents) - {SUPERVISOR}
    assert set(found.tools_used) == {
        "windops_get_evidence", "windops_get_turbine", "documentation_search_documents",
        "windops_get_case", "windops_list_cases", "weather_get_forecast",
    }  # fmt: skip
    # Each agent's tokens were its own. Web research was only described, never called.
    assert {agent for agent, _, _ in tokens.asked} == set(TEAM.agents)
