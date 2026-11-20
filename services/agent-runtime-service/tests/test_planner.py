"""Step 12: the Maintenance Planner's proposal and draft (M9.3), with scripted models and tools.

No call reaches Claude or an MCP server here. make planner-check drafts for real.
"""

import asyncio
import functools
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from team_support import BOUND, Scripted, Tokens, call, mcp_names, sandbox_run, toolbox
from windops_agent_runtime.team import planner, rehearsal
from windops_agent_runtime.team.definitions import PLANNER, team
from windops_agent_runtime.team.run import TeamRunner
from windops_agent_runtime.team.standin import PLACEHOLDER_KEY, StandIn
from windops_claude import BudgetExhaustedError, Claude, MemoryLedger, Meter, load, models

POLICY = load()
TEAM = team()
CASE, SNAPSHOT, TURBINE = "case_" + "a" * 20, "evs_" + "b" * 20, "dogger-east-t01"
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
ORDER = "wo_" + "d" * 20
INSPECTION = {"action": "inspection", "summary": "Inspect the oil cooler fan",
              "requires_climb": True, "downtime_hours": 6.0, "impact_result_id": ""}  # fmt: skip


class Tools:
    """WindOps MCP's answers: every tool, with a draft that may be refused."""

    def __init__(self, refuse: str = "") -> None:
        self.refuse, self.drafts = refuse, 0

    async def anything(self, arguments: dict[str, Any]) -> str:
        return json.dumps({"turbine_id": TURBINE, "parts": []})

    async def draft(self, arguments: dict[str, Any]) -> str:
        self.drafts += 1
        if self.refuse:
            return json.dumps({"error": self.refuse})
        return json.dumps({"work_order_id": ORDER, "status": "draft", "revision": 1,
                           "case_id": CASE, "drafted_by_agent": "agent:maintenance-planner",
                           "passed": False, "policy_outcome": "escalate",
                           "problems": [{"field": "procedures", "code": "missing",
                                         "message": "Cite one", "blocking": True}]})  # fmt: skip

    def answers(self) -> dict[str, Any]:
        every = dict.fromkeys(mcp_names(TEAM.agents.values()), self.anything)
        return {**every, planner.DRAFT_TOOL: self.draft}


def runner(tools: Tools, claude: Claude | None = None, tool_calls: int = 50,
           model: Any = None) -> TeamRunner:  # fmt: skip
    claude = claude or Claude(POLICY, Meter(POLICY.budgets.default, MemoryLedger()), lambda: "k")
    return TeamRunner(TEAM, toolbox(tools.answers()), claude=claude, tokens=Tokens(),
                      tool_calls=tool_calls, sandbox_run=sandbox_run(), model=model)  # fmt: skip


def briefing(procedures: tuple[str, ...] = ("kn:GBX-OIL-01@3#12",)) -> str:
    return planner.brief(
        case_id=CASE, snapshot_id=SNAPSHOT, turbine_id=TURBINE,
        result={"observed_facts": [{"statement": "Oil hot", "citations": ["tel_1"]}]},
        action=INSPECTION, policy={"outcome": "escalate", "result_id": "pol_1"},
        procedures=procedures, now=NOW,
    )  # fmt: skip


def test_the_first_action_policy_did_not_block_is_drafted() -> None:
    actions = [{"action": "replacement"}, {"action": "inspection"}, {"action": "monitoring"}]
    outcomes = [{"outcome": "block"}, {"outcome": "escalate"}, {"outcome": "pass"}]
    assert planner.choose(actions, outcomes) == (actions[1], outcomes[1])
    assert planner.choose(actions[:1], outcomes[:1]) is None
    assert planner.choose([], []) is None
    # A climb has no window to judge the weather on until it is drafted with one.
    weather = [{"outcome": "block", "window_only": True}]
    assert planner.choose(actions[:1], weather) == (actions[0], weather[0])


def test_only_approved_passages_the_run_read_may_be_cited_as_procedures() -> None:
    assert planner.procedures_of(["tel_1", "kn:GBX-OIL-01@3#12", "pol_1"]) == ["kn:GBX-OIL-01@3#12"]
    shown = briefing(())
    assert "(none was read in this run)" in shown and "Time now: 2026-11-20T09:00:00+00:00" in shown


def test_in_the_investigation_the_planner_advises_without_its_drafting_tool() -> None:
    """M9.3: a work order is drafted only in step 12, after the result's checks and policy."""
    scripts = {
        "supervisor": [call("task", subagent_type=PLANNER, description=f"Plan for {CASE}"),
                       AIMessage("Report.")],
        PLANNER: [AIMessage("An inspection would take a crew of two for six hours.")],
    }  # fmt: skip

    def model(role: str) -> Scripted:
        return Scripted(role=role, script=scripts.setdefault(role, [AIMessage("ok")]))

    asyncio.run(runner(Tools(), model=model).investigate(CASE, SNAPSHOT))
    assert "windops_find_parts" in BOUND[PLANNER]
    assert planner.DRAFT_TOOL not in BOUND[PLANNER]


def test_the_planner_drafts_once_and_a_second_draft_never_reaches_the_server() -> None:
    tools = Tools()
    arguments = rehearsal.draft_arguments(CASE, INSPECTION, NOW)
    script = [call("windops_find_parts", turbine_id=TURBINE),
              call(planner.DRAFT_TOOL, **arguments),
              call(planner.DRAFT_TOOL, **arguments),
              AIMessage("Proposal: inspect the fan; draft wo_ for review.")]  # fmt: skip
    drafted = asyncio.run(runner(tools).draft_work_order(
        briefing(), used=10, model=lambda role: Scripted(role=role, script=script),
    ))  # fmt: skip
    assert tools.drafts == 1
    assert drafted.work_order is not None and drafted.work_order["work_order_id"] == ORDER
    assert drafted.work_order["problems"][0]["code"] == "missing"
    assert drafted.proposal.startswith("Proposal:")
    assert drafted.tools_used == ["windops_find_parts", planner.DRAFT_TOOL]
    assert planner.DRAFT_TOOL in BOUND[PLANNER]
    # The rehearsal's draft starts in the future and plans the action it was given.
    assert arguments["action"] == "inspection" and arguments["planned_start"] > NOW.isoformat()


def test_a_refused_draft_leaves_no_work_order_and_says_why() -> None:
    """An operator's investigation: the Maintenance Service refuses the agent's draft, as its
    principal may not plan. The run carries on without one."""
    script = [call(planner.DRAFT_TOOL, **rehearsal.draft_arguments(CASE, INSPECTION, NOW)),
              AIMessage("I could not draft it.")]  # fmt: skip
    drafted = asyncio.run(runner(Tools(refuse="You do not have permission")).draft_work_order(
        briefing(), used=0, model=lambda role: Scripted(role=role, script=script),
    ))  # fmt: skip
    assert drafted.work_order is None and drafted.refused == "You do not have permission"
    silent = asyncio.run(runner(Tools()).draft_work_order(
        briefing(), used=0, model=lambda role: Scripted(role=role, script=[AIMessage("No.")]),
    ))  # fmt: skip
    assert silent.work_order is None and silent.refused == "The planner drafted no work order"


def test_drafting_spends_only_what_is_left_of_the_runs_tool_budget() -> None:
    script = [call("windops_list_cases", turbine_id=TURBINE), AIMessage("done")]
    with pytest.raises(BudgetExhaustedError):
        asyncio.run(runner(Tools(), tool_calls=50).draft_work_order(
            briefing(), used=50, model=lambda role: Scripted(role=role, script=script),
        ))  # fmt: skip


def test_a_dry_run_drafts_through_the_real_claude_client() -> None:
    """The stand-in API plays step 12's planner as the rehearsal does (ADR 0027)."""
    ledger, tools = MemoryLedger(), Tools()

    async def scenario() -> Any:
        stand_in, stop = StandIn(), asyncio.Event()
        serving = asyncio.create_task(stand_in.serve(stop))
        while stand_in.url is None:
            await asyncio.sleep(0.01)
        claude = Claude(
            POLICY, Meter(POLICY.budgets.default, ledger), lambda: PLACEHOLDER_KEY,
            make_model=functools.partial(models.chat_model, base_url=stand_in.url),
            provider="stand-in",
        )  # fmt: skip
        try:
            return await runner(tools, claude=claude).draft_work_order(briefing(), used=0)
        finally:
            stop.set()
            await serving

    drafted = asyncio.run(scenario())
    assert drafted.tools_used == ["windops_list_cases", "windops_find_parts", planner.DRAFT_TOOL]
    assert drafted.work_order is not None and tools.drafts == 1
    assert drafted.proposal.startswith("Stand-in proposal")
    assert {c.role for c in ledger.calls} == {PLANNER} and len(ledger.calls) == 4
