"""The investigation's structured result, and the checks it must pass (SRS 5.7, M8.8, ADR 0022).

The Supervisor's report is turned into a `Draft` through Claude's structured outputs. The draft is
then checked, deterministically, against what the team actually saw:

- facts, hypotheses, contradictions, gaps and next steps are kept apart (FR-INV-003);
- every fact and hypothesis cites at least one reference, and every reference is one a tool
  returned in this run: an evidence item's record ID, a document's citation, a research source,
  a case, or an estimate or policy result (FR-INV-004);
- a contradiction names at least two sources, and missing information is a data gap, never an
  invented answer (FR-INV-005);
- every impact figure is exactly the Production Impact Service's own (FR-IMP-004).

A draft that fails is sent back with its problems, at most twice. The constraints live here, not
in the schema, so the schema stays one Claude's structured outputs accept as it is.
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

Action = Literal["inspection", "repair", "replacement", "adjustment", "monitoring"]
# The keys whose values a tool returns as something a claim may cite.
_CITABLE_KEYS = frozenset(
    {"record_id", "citation", "source_id", "url", "result_id", "case_id", "snapshot_id",
     "alarm_id", "turbine_id", "forecast_id"}
)  # fmt: skip
_KN = re.compile(r"kn:[A-Za-z0-9_.:@#-]+")


class Claim(BaseModel):
    """An observed fact, with what shows it."""

    statement: str = Field(description="One fact, as observed")
    citations: list[str] = Field(description="Reference IDs that show it, as the tools gave them")


class Hypothesis(BaseModel):
    statement: str = Field(description="A possible explanation, never presented as a fact")
    support: list[str] = Field(description="Reference IDs that support it")
    against: list[str] = Field(description="Reference IDs that argue against it, if any")
    confidence: float = Field(description="From 0 to 1")


class Conflict(BaseModel):
    description: str = Field(description="What disagrees, without resolving it")
    citations: list[str] = Field(description="The disagreeing sources: at least two")


class Figure(BaseModel):
    name: str
    value: float
    low: float
    high: float
    unit: str


class Impact(BaseModel):
    result_id: str = Field(description="The estimate's result ID, exactly as given")
    figures: list[Figure] = Field(description="Every figure exactly as the estimate gave it")


class Recommendation(BaseModel):
    action: Action
    summary: str
    requires_climb: bool
    downtime_hours: float | None = None
    impact_result_id: str = Field(default="", description="The estimate for this action, if any")


class Draft(BaseModel):
    """What the Supervisor reports, in SRS 5.7's shape."""

    case_id: str
    asset_id: str = Field(description="The turbine's ID")
    snapshot_id: str
    observed_facts: list[Claim]
    hypotheses: list[Hypothesis]
    evidence_references: list[str] = Field(description="Every reference the result relies on")
    conflicting_evidence: list[Conflict]
    data_gaps: list[str] = Field(description="What is missing, and why it matters")
    risk_flags: list[str]
    recommended_actions: list[Recommendation]
    impact: Impact | None = None
    confidence: float = Field(description="Overall, from 0 to 1")
    explanation: str = Field(description="A concise rationale, never private reasoning")
    escalation_required: bool


class Seen:
    """What the team's tools returned in this run: what a claim may cite, and the estimates."""

    def __init__(self) -> None:
        self.references: set[str] = set()
        self.estimates: dict[str, list[dict[str, Any]]] = {}

    def add(self, tool: str, text: str) -> None:
        self.references.update(_KN.findall(text))
        try:
            found = json.loads(text)
        except ValueError:
            return
        self._walk(found)
        if tool == "windops_estimate_impact" and isinstance(found, dict) and found.get("result_id"):
            self.estimates[str(found["result_id"])] = list(found.get("figures") or [])

    def _walk(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _CITABLE_KEYS and isinstance(item, str) and item:
                    self.references.add(item)
                self._walk(item)
        elif isinstance(value, list):
            for item in value:
                self._walk(item)

    def as_dict(self) -> dict[str, Any]:
        return {"references": sorted(self.references), "estimates": self.estimates}

    @classmethod
    def from_dict(cls, stored: Mapping[str, Any]) -> "Seen":
        seen = cls()
        seen.references = set(stored.get("references", ()))
        seen.estimates = dict(stored.get("estimates", {}))
        return seen


def _unknown(cited: Iterable[str], seen: Seen) -> list[str]:
    return sorted({c for c in cited if c not in seen.references})


def problems(draft: Draft, *, case_id: str, snapshot_id: str, seen: Seen) -> list[str]:
    """Everything wrong with a draft. An empty list means it may be kept."""
    found: list[str] = []
    if draft.case_id != case_id or draft.snapshot_id != snapshot_id:
        found.append(f"case_id and snapshot_id must be {case_id} and {snapshot_id}")
    if not draft.observed_facts and not draft.data_gaps:
        found.append("Report observed facts, or say in data_gaps why there are none")
    for index, fact in enumerate(draft.observed_facts):
        if not fact.citations:
            found.append(f"observed_facts[{index}] cites nothing: cite it, or make it a hypothesis")
        elif unknown := _unknown(fact.citations, seen):
            found.append(f"observed_facts[{index}] cites references no tool returned: {unknown}")
    for index, hypothesis in enumerate(draft.hypotheses):
        if not hypothesis.support:
            found.append(f"hypotheses[{index}] has no support")
        if unknown := _unknown([*hypothesis.support, *hypothesis.against], seen):
            found.append(f"hypotheses[{index}] cites references no tool returned: {unknown}")
        if not 0 <= hypothesis.confidence <= 1:
            found.append(f"hypotheses[{index}].confidence must be from 0 to 1")
    for index, conflict in enumerate(draft.conflicting_evidence):
        if len(set(conflict.citations)) < 2:
            found.append(f"conflicting_evidence[{index}] must name at least two sources")
        elif unknown := _unknown(conflict.citations, seen):
            found.append(f"conflicting_evidence[{index}] cites unknown references: {unknown}")
    if unknown := _unknown(draft.evidence_references, seen):
        found.append(f"evidence_references holds references no tool returned: {unknown}")
    if not 0 <= draft.confidence <= 1:
        found.append("confidence must be from 0 to 1")
    found += _impact_problems(draft, seen)
    return found


def _impact_problems(draft: Draft, seen: Seen) -> list[str]:
    """FR-IMP-004: an agent explains the estimate's figures, and never changes one."""
    found = []
    if draft.impact is not None:
        given = seen.estimates.get(draft.impact.result_id)
        if given is None:
            found.append(
                f"impact.result_id {draft.impact.result_id} is not an estimate this run got"
            )
        else:
            expected = {f["name"]: f for f in given}
            for figure in draft.impact.figures:
                source = expected.get(figure.name)
                if source is None or any(
                    getattr(figure, key) != source[key] for key in ("value", "low", "high", "unit")
                ):
                    found.append(
                        f"impact figure {figure.name} differs from the estimate: give it exactly"
                    )
    for index, action in enumerate(draft.recommended_actions):
        if action.impact_result_id and action.impact_result_id not in seen.estimates:
            found.append(f"recommended_actions[{index}] names an estimate this run did not get")
    return found
