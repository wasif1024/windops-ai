"""A scripted stand-in for Claude, to prove the team end to end without spending (M8.7).

A run asked to rehearse assembles the real team, with real tools, tokens and MCP servers, and
swaps only the model: each agent follows a fixed script instead of asking Claude. The Supervisor
reads the evidence, delegates to each specialist once, and reports; each specialist makes one
real tool call, from its own allowlist, and answers. In step 12 the Maintenance Planner reads the
turbine's history, finds parts, and drafts the recommended inspection as a real draft work order
(M9.3). No call reaches Claude, and no tool that costs anything is used: web research is left
out. Only when asked, the Reliability Engineer also runs a real Sandbox task on a synthetic
vibration, which costs a little LangSmith compute.

The run with Claude itself is the M8.13 exit gate's.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from windops_agent_runtime.team.metered import PROVIDER
from windops_agent_runtime.team.result import Claim, Draft, Recommendation, Seen

REPORT = "Rehearsal report: every specialist answered, and nothing was concluded."
PROPOSAL = "Rehearsal proposal: the recommended action, drafted as a work order for a person."


def _call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


# A synthetic vibration: 37 Hz, and a weaker 91 Hz, sampled at 512 Hz.
RATE, TONES = 512, ((37.0, 1.0), (91.0, 0.4))


def vibration() -> list[float]:
    return [
        round(sum(amp * math.sin(2 * math.pi * hz * t / RATE) for hz, amp in TONES), 6)
        for t in range(RATE)
    ]


def scripts(
    case_id: str, snapshot_id: str, turbine_id: str, *, sandbox: bool = False
) -> dict[str, list[AIMessage]]:
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
    if sandbox:
        spectrum = _call("sandbox_vibration_spectrum", sample_rate_hz=RATE, samples=vibration())
        plans["reliability-engineer"].insert(1, spectrum)
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


def models(
    case_id: str, snapshot_id: str, turbine_id: str, *, sandbox: bool = False
) -> Callable[[str], BaseChatModel]:
    planned = scripts(case_id, snapshot_id, turbine_id, sandbox=sandbox)

    def make(role: str) -> BaseChatModel:
        return RehearsalModel(role=role, script=planned.get(role, []))

    return make


def draft_arguments(case_id: str, action: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The rehearsal's work order for an action: one task, a window two days out, no parts and no
    procedure. An inspection or monitoring needs none; anything else is drafted, and its
    validation says what is missing."""
    start = (now + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    hours = float(action.get("downtime_hours") or 4)
    return {
        "case_id": case_id, "title": f"Rehearsal: {action['action']} for {case_id}"[:200],
        "action": action["action"], "summary": str(action.get("summary") or "Rehearsal")[:4000],
        "tasks": [{"description": str(action.get("summary") or "Rehearsal task")[:2000],
                   "duration_hours": hours, "crew_size": 2,
                   "requires_climb": bool(action.get("requires_climb"))}],
        "parts": [], "procedures": [], "planned_start": start.isoformat(),
        "planned_end": (start + timedelta(hours=hours)).isoformat(),
        "estimated_downtime_hours": hours,
        "impact_result_id": str(action.get("impact_result_id") or ""),
    }  # fmt: skip


def drafting(
    case_id: str, turbine_id: str, action: Mapping[str, Any], now: datetime
) -> Callable[[str], BaseChatModel]:
    """Step 12's planner, scripted: its history, its parts, one draft, and its proposal."""
    script = [
        _call("windops_list_cases", turbine_id=turbine_id, limit=5),
        _call("windops_find_parts", turbine_id=turbine_id, limit=5),
        _call("windops_create_draft_work_order", **draft_arguments(case_id, action, now)),
        AIMessage(PROPOSAL),
    ]

    def make(role: str) -> BaseChatModel:
        return RehearsalModel(role=role, script=script if role == "maintenance-planner" else [])

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
