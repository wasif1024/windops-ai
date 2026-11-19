"""A scripted stand-in for Claude, to prove the team end to end without spending (M8.7).

A run asked to rehearse assembles the real team, with real tools, tokens and MCP servers, and
swaps only the model: each agent follows a fixed script instead of asking Claude. The Supervisor
reads the evidence, delegates to each specialist once, and reports; each specialist makes one
real tool call, from its own allowlist, and answers. No call reaches Claude, and no tool that
costs anything is used: web research is left out.

The run with Claude itself is the M8.13 exit gate's.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from windops_agent_runtime.team.metered import PROVIDER
from windops_agent_runtime.team.result import Claim, Draft, Recommendation, Seen

REPORT = "Rehearsal report: every specialist answered, and nothing was concluded."


def _call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


def scripts(case_id: str, snapshot_id: str, turbine_id: str) -> dict[str, list[AIMessage]]:
    """Each agent's turns: one real, free tool call from its own allowlist, then an answer."""
    start = datetime.now(UTC) + timedelta(hours=1)
    specialists: dict[str, AIMessage] = {
        "scada-analyst": _call("windops_get_turbine", turbine_id=turbine_id),
        "reliability-engineer": _call(
            "documentation_search_documents", query="gearbox oil temperature high", limit=2
        ),
        "production-impact": _call("windops_get_case", case_id=case_id),
        "maintenance-planner": _call("windops_list_cases", turbine_id=turbine_id, limit=5),
        "safety-researcher": _call(
            "weather_get_forecast",
            turbine_id=turbine_id,
            from_time=start.isoformat(),
            to_time=(start + timedelta(hours=6)).isoformat(),
        ),
    }
    supervisor = [_call("windops_get_evidence", snapshot_id=snapshot_id, limit=5)]
    supervisor += [
        _call("task", subagent_type=name, description=f"Rehearsal question for {name} on {case_id}")
        for name in specialists
    ]
    supervisor.append(AIMessage(REPORT))
    plans = {name: [first, AIMessage(f"{name}: rehearsed")] for name, first in specialists.items()}
    return {"supervisor": supervisor, **plans}


class RehearsalModel(BaseChatModel):
    """Answers from a script. Its provider is the platform's, so Deep Agents restricts it the
    same way as the real one."""

    role: str
    script: list[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "windops-rehearsal"

    @property
    def model_name(self) -> str:
        return self.role

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> Any:
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = PROVIDER
        return params

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:  # fmt: skip
        turn = self.script.pop(0) if self.script else AIMessage("Rehearsal over.")
        return ChatResult(generations=[ChatGeneration(message=turn)])


def models(case_id: str, snapshot_id: str, turbine_id: str) -> Callable[[str], BaseChatModel]:
    planned = scripts(case_id, snapshot_id, turbine_id)

    def make(role: str) -> BaseChatModel:
        return RehearsalModel(role=role, script=planned.get(role, []))

    return make


def draft(case_id: str, snapshot_id: str, turbine_id: str, seen: Seen) -> Draft:
    """The result a rehearsal reports: built from what its tools returned, without Claude, so the
    checks, and the policy check that follows, still run for real."""
    cited = sorted(r for r in seen.references if r not in (case_id, snapshot_id, turbine_id))
    first = cited[:1] or [snapshot_id]
    return Draft(
        case_id=case_id, asset_id=turbine_id, snapshot_id=snapshot_id,
        observed_facts=[Claim(statement="The rehearsal read the case's evidence", citations=first)],
        hypotheses=[], evidence_references=first, conflicting_evidence=[],
        data_gaps=["A rehearsal draws no conclusions: no model reasoned over the evidence."],
        risk_flags=[],
        recommended_actions=[Recommendation(
            action="inspection", summary="Rehearsal: inspect the component the case concerns",
            requires_climb=True,
        )],
        confidence=0.0, explanation="A rehearsal of the team, with no model.",
        escalation_required=True,
    )  # fmt: skip
