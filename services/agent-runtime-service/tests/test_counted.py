"""The team's tool calls and handoffs are counted for the Agent Operations dashboard (SRS 10.3)."""

import asyncio
import json
from typing import Any

import pytest
from team_support import Tokens, sandbox_run, toolbox
from windops_agent_runtime.team import counted
from windops_agent_runtime.team.tools import outcome_of


class Recorder:
    def __init__(self) -> None:
        self.seen: list[tuple[int, dict[str, Any]]] = []

    def add(self, value: int, attributes: dict[str, Any]) -> None:
        self.seen.append((value, attributes))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(counted, "tool_calls", recorder)
    return recorder


def test_an_answer_that_is_the_servers_error_is_an_error() -> None:
    assert outcome_of(json.dumps({"turbine_id": "t1"})) == "ok"
    assert outcome_of(json.dumps({"error": "denied"})) == "error"
    assert outcome_of(json.dumps({"error": "x", "detail": "kept"})) == "ok"
    assert outcome_of("plain text") == "ok"


def test_each_tool_call_is_counted_by_agent_tool_and_outcome(calls: Recorder) -> None:
    async def turbine(arguments: dict[str, Any]) -> str:
        return json.dumps({"turbine_id": "t1"})

    async def refused(arguments: dict[str, Any]) -> str:
        return json.dumps({"error": "Not allowed"})

    async def broken(arguments: dict[str, Any]) -> str:
        raise ConnectionError("down")

    box = toolbox({"windops_get_turbine": turbine, "windops_get_case": refused,
                   "windops_list_alarms": broken})  # fmt: skip

    async def scenario() -> None:
        tools = await box.tools_for(
            "scada-analyst",
            ("mcp.windops.get_turbine", "mcp.windops.get_case", "mcp.windops.list_alarms"),
            Tokens(),
        )
        by_name = {tool.name: tool for tool in tools}
        await by_name["windops_get_turbine"].ainvoke({})
        await by_name["windops_get_case"].ainvoke({})
        with pytest.raises(ConnectionError):
            await by_name["windops_list_alarms"].ainvoke({})

    asyncio.run(scenario())
    assert [(a["tool"], a["outcome"]) for _, a in calls.seen] == [
        ("get_turbine", "ok"), ("get_case", "error"), ("list_alarms", "failed"),
    ]  # fmt: skip
    assert {(a["agent"], a["server"]) for _, a in calls.seen} == {
        ("scada-analyst", "windops-mcp-windops")
    }


def test_a_sandbox_task_is_counted_as_a_tool_call(calls: Recorder) -> None:
    box = toolbox({})

    async def scenario() -> None:
        (tool,) = await box.tools_for(
            "reliability-engineer", ("sandbox.trend_fit",), Tokens(), sandbox_run=sandbox_run()
        )
        await tool.ainvoke({"name": tool.name, "args": {}, "id": "c1", "type": "tool_call"})

    asyncio.run(scenario())
    ((_, labels),) = calls.seen
    assert (labels["server"], labels["tool"], labels["agent"]) == (
        "sandbox", "trend_fit", "reliability-engineer",
    )  # fmt: skip
