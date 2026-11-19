"""A dry run: the whole team on the real Claude client, over HTTP, against the stand-in API
(M8.13, ADR 0027). Everything a rehearsal skips is exercised here, at no cost: the client, its
requests and tools, thinking replayed through the history guard, step 10's structured outputs,
the budget and the ledger."""

import asyncio
import functools
import json
from typing import Any

from team_support import Tokens, mcp_names, sandbox_run, toolbox
from windops_agent_runtime.team.definitions import SUPERVISOR, team
from windops_agent_runtime.team.result import Seen
from windops_agent_runtime.team.run import TeamRunner
from windops_agent_runtime.team.standin import PLACEHOLDER_KEY, StandIn, answer
from windops_claude import Claude, MemoryLedger, Meter, load, models

POLICY = load()
TEAM = team()
CASE, SNAPSHOT, TURBINE = "case_" + "a" * 20, "evs_" + "b" * 20, "dogger-east-t01"


async def evidence(arguments: dict[str, Any]) -> str:
    items = [{"record_id": "alm_gearbox_1"}, {"record_id": "tsnap_1"}]
    snapshot = arguments.get("snapshot_id")
    return json.dumps({"snapshot_id": snapshot, "turbine_id": TURBINE, "items": items})


async def anything(arguments: dict[str, Any]) -> str:
    return json.dumps({"turbine_id": TURBINE, "record_id": "doc_passage_1"})


def test_a_dry_run_takes_the_team_through_the_real_claude_client() -> None:
    ledger = MemoryLedger()
    tokens = Tokens()

    async def scenario() -> tuple[Any, Any, list[Any]]:
        stand_in, stop = StandIn(), asyncio.Event()
        serving = asyncio.create_task(stand_in.serve(stop))
        while stand_in.url is None:
            await asyncio.sleep(0.01)
        answers = dict.fromkeys(mcp_names(TEAM.agents.values()), anything)
        answers["windops_get_evidence"] = evidence
        claude = Claude(
            POLICY, Meter(POLICY.budgets.default, ledger), lambda: PLACEHOLDER_KEY,
            make_model=functools.partial(models.chat_model, base_url=stand_in.url),
            provider="stand-in",
        )  # fmt: skip
        runner = TeamRunner(TEAM, toolbox(answers), claude=claude, tokens=tokens,
                            tool_calls=50, sandbox_run=sandbox_run())  # fmt: skip
        try:
            found = await runner.investigate(CASE, SNAPSHOT)
            composed = await runner.compose(CASE, SNAPSHOT, found.report,
                                            Seen.from_dict(found.seen))  # fmt: skip
        finally:
            stop.set()
            await serving
        return found, composed, ledger.calls

    found, composed, calls = asyncio.run(scenario())
    # Every specialist was handed work, each made its one tool call with its own token.
    assert set(found.delegated) == set(TEAM.agents) - {SUPERVISOR}
    assert {"windops_get_evidence", "windops_get_turbine", "documentation_search_documents",
            "windops_get_case", "windops_list_cases", "weather_get_forecast"} <= set(
        found.tools_used)  # fmt: skip
    assert {agent for agent, _, _ in tokens.asked} == set(TEAM.agents)
    # Step 10's structured result, through Claude's structured outputs, passed its checks
    # without a repair, and cites only what the tools returned.
    draft = composed.draft
    assert composed.repairs == 0
    assert set(draft.evidence_references) <= {"alm_gearbox_1", "tsnap_1", "doc_passage_1"}
    assert draft.case_id == CASE and draft.asset_id == TURBINE
    # Every call went through the policy: recorded, with each role's own model and effort.
    roles = {call.role for call in calls}
    assert roles >= {"supervisor"} and len(calls) >= 1 + 2 * (len(TEAM.agents) - 1)
    for call in calls:
        role = POLICY.role(call.role)
        assert (call.requested_model, call.effort) == (role.model, role.effort)
        assert call.stop_reason in ("tool_use", "end_turn")


def test_the_stand_in_never_answers_as_more_than_a_script() -> None:
    """It plays the rehearsal's moves, and gives step 10 a result built from the brief only."""
    brief = ("Case: " + CASE + "\nSnapshot: " + SNAPSHOT +
             "\n\nReferences you may cite, exactly as written:\nalm_1\n" + TURBINE +
             "\n\nThe Supervisor's report:\nStand-in report on turbine " + TURBINE)  # fmt: skip
    body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": brief}],
            "output_config": {"format": {"type": "json_schema", "schema": {}}}}  # fmt: skip
    got = answer(body)
    text = next(b["text"] for b in got["content"] if b["type"] == "text")
    result = json.loads(text)
    assert result["evidence_references"] == ["alm_1"] and result["asset_id"] == TURBINE
    assert got["content"][0]["type"] == "thinking" and got["stop_reason"] == "end_turn"
    assert got["model"] == "claude-opus-5-5"
