"""Each agent's tool allowlist, as the Identity Service enforces it, against the MCP servers' own
tool definitions (SRS 5.3, MCP-002, MCP-006, ADR 0020)."""

import importlib
from typing import Any

import pytest
from windops_identity import targets
from windops_identity.targets import (
    AGENT_TOOLS,
    EXTERNAL_TOOLS,
    MCP_TOOLS,
    SANDBOX_TASKS,
    WRITE_TOOLS,
    check_allowlists,
    sandbox_scope,
    tool_scope,
)


def defined() -> dict[str, Any]:
    """Every tool every MCP server defines, by its scope."""
    found = {}
    for server in MCP_TOOLS:
        module = importlib.import_module(f"windops_mcp_{server}.tools")
        for tool in module.tools(object()):
            found[tool_scope(tool.server, tool.name)] = tool
    return found


def test_the_identity_service_knows_every_tool_each_server_defines() -> None:
    tools = defined()
    assert set(tools) == {tool_scope(s, t) for s, names in MCP_TOOLS.items() for t in names}


def test_external_and_writing_tools_are_classed_as_the_servers_define_them() -> None:
    tools = defined()
    assert {scope for scope, tool in tools.items() if tool.external} == EXTERNAL_TOOLS
    assert {scope for scope, tool in tools.items() if not tool.read_only} == WRITE_TOOLS


def test_no_agent_that_reads_external_content_may_write() -> None:
    """MCP-006."""
    for agent, tools in AGENT_TOOLS.items():
        assert not (tools & EXTERNAL_TOOLS and tools & WRITE_TOOLS), agent


def test_the_allowlists_follow_srs_5_3() -> None:
    def servers(agent: str) -> set[str]:
        return {scope.split(".")[1] for scope in AGENT_TOOLS[agent]}

    assert set(AGENT_TOOLS) == {
        "supervisor", "scada-analyst", "reliability-engineer", "production-impact",
        "maintenance-planner", "safety-researcher",
    }  # fmt: skip
    # The planner never reads raw web content, and only the safety researcher searches the web.
    assert "tavily" not in servers("maintenance-planner")
    assert {a for a in AGENT_TOOLS if "tavily" in servers(a)} == {"safety-researcher"}
    # Only the production-impact agent asks for estimates, and only the supervisor checks policy.
    estimate, check = (
        tool_scope("windops", "estimate_impact"),
        tool_scope("windops", "check_policy"),
    )
    assert {a for a, t in AGENT_TOOLS.items() if estimate in t} == {"production-impact"}
    assert {a for a, t in AGENT_TOOLS.items() if check in t} == {"supervisor"}
    # Development servers are never an agent's.
    assert not any(servers(a) & {"filesystem", "postgres"} for a in AGENT_TOOLS)


@pytest.mark.parametrize(
    "change",
    [
        {
            "safety-researcher": frozenset(
                {tool_scope("tavily", "search_web"), "mcp.windops.create_draft"}
            )
        },
        {"supervisor": frozenset({"mcp.windops.approve"})},
        {"scada-analyst": frozenset({tool_scope("filesystem", "read_file")})},
    ],
)
def test_a_bad_allowlist_stops_the_service_from_starting(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, frozenset[str]]
) -> None:
    monkeypatch.setattr(targets, "AGENT_TOOLS", {**AGENT_TOOLS, **change})
    monkeypatch.setattr(
        targets, "MCP_TOOLS", {**MCP_TOOLS, "windops": (*MCP_TOOLS["windops"], "create_draft")}
    )
    monkeypatch.setattr(targets, "WRITE_TOOLS", frozenset({"mcp.windops.create_draft"}))
    with pytest.raises(ValueError):
        check_allowlists()


def test_each_agents_definition_names_exactly_its_allowlist() -> None:
    """An agent's definition and its tokens can never drift apart (M8.7, ADR 0021)."""
    from windops_agent_runtime.team.definitions import team

    agents = team().agents
    assert set(agents) == set(AGENT_TOOLS)
    for name, agent in agents.items():
        assert set(agent.tools) == AGENT_TOOLS[name], name


def test_the_sandbox_tasks_on_allowlists_are_the_adapters_registered_tasks() -> None:
    """ADR 0024: only the Reliability Engineer runs Sandbox tasks, and only registered ones."""
    from windops_agent_sandbox.tasks import registry, tasks_root

    assert set(SANDBOX_TASKS) == set(registry(tasks_root()))
    assert {
        a for a, tools in AGENT_TOOLS.items() if any(t.startswith("sandbox.") for t in tools)
    } == {"reliability-engineer"}
    assert {t for t in AGENT_TOOLS["reliability-engineer"] if t.startswith("sandbox.")} == {
        sandbox_scope(task) for task in SANDBOX_TASKS
    }
