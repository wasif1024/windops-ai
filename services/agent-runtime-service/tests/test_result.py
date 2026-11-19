"""The structured result's checks, and step 10's repairs, with scripted answers (M8.8)."""

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from windops_agent_runtime.team.compose import InvalidResultError, compose
from windops_agent_runtime.team.result import Draft, Seen, problems
from windops_claude import Budget, Claude, MemoryLedger, Meter, load

CASE, SNAPSHOT = "case_" + "a" * 20, "evs_" + "b" * 20
POLICY = load()
ESTIMATE = {"name": "lost_energy_mwh", "value": 40.0, "low": 30.0, "high": 50.0, "unit": "MWh"}


def seen() -> Seen:
    found = Seen()
    found.add("windops_get_evidence", json.dumps(
        {"snapshot_id": SNAPSHOT, "items": [{"record_id": "tel_1"}, {"record_id": "alm_2"}]}
    ))  # fmt: skip
    found.add("documentation_read_passage", json.dumps({"passage": {"citation": "kn:GBX@3#12"}}))
    found.add("windops_estimate_impact", json.dumps({"result_id": "imp_1", "figures": [ESTIMATE]}))
    return found


def draft(**changes: Any) -> Draft:
    base: dict[str, Any] = {
        "case_id": CASE, "asset_id": "dogger-east-t01", "snapshot_id": SNAPSHOT,
        "observed_facts": [{"statement": "Gearbox oil rose to 88 degC", "citations": ["tel_1"]}],
        "hypotheses": [{"statement": "Oil cooling failure", "support": ["tel_1", "kn:GBX@3#12"],
                        "against": [], "confidence": 0.6}],
        "evidence_references": ["tel_1", "kn:GBX@3#12"],
        "conflicting_evidence": [], "data_gaps": [], "risk_flags": [],
        "recommended_actions": [{"action": "inspection", "summary": "Inspect the oil cooler",
                                 "requires_climb": True, "impact_result_id": "imp_1"}],
        "impact": {"result_id": "imp_1", "figures": [ESTIMATE]},
        "confidence": 0.6, "explanation": "Oil temperature rose with load.",
        "escalation_required": False,
    }  # fmt: skip
    base.update(changes)
    return Draft.model_validate(base)


def problems_of(candidate: Draft) -> list[str]:
    return problems(candidate, case_id=CASE, snapshot_id=SNAPSHOT, seen=seen())


def test_a_cited_draft_with_the_estimates_own_figures_passes() -> None:
    assert problems_of(draft()) == []
    assert {"tel_1", "alm_2", "kn:GBX@3#12", "imp_1", SNAPSHOT} <= seen().references


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"observed_facts": [{"statement": "Oil is fine", "citations": []}]}, "cites nothing"),
        (
            {"observed_facts": [{"statement": "Oil is fine", "citations": ["tel_99"]}]},
            "no tool returned",
        ),
        (
            {"conflicting_evidence": [{"description": "x", "citations": ["tel_1"]}]},
            "at least two sources",
        ),
        ({"case_id": "case_" + "z" * 20}, "case_id and snapshot_id"),
        ({"observed_facts": [], "data_gaps": []}, "Report observed facts"),
        ({"confidence": 1.5}, "confidence must be"),
        ({"evidence_references": ["kn:OTHER@1#1"]}, "evidence_references"),
    ],
)
def test_a_draft_that_breaks_frinv_003_to_005_is_refused(
    changes: dict[str, Any], expected: str
) -> None:
    assert any(expected in problem for problem in problems_of(draft(**changes)))


@pytest.mark.parametrize(
    "impact",
    [
        {"result_id": "imp_1", "figures": [{**ESTIMATE, "value": 41.0}]},
        {"result_id": "imp_1", "figures": [{**ESTIMATE, "unit": "kWh"}]},
        {"result_id": "imp_1", "figures": [{**ESTIMATE, "name": "lost_revenue_eur"}]},
        {"result_id": "imp_9", "figures": [ESTIMATE]},
    ],
)
def test_an_impact_figure_must_be_the_estimates_own(impact: dict[str, Any]) -> None:
    """FR-IMP-004: the Production Impact agent explains figures and never changes one."""
    assert any("impact" in problem for problem in problems_of(draft(impact=impact)))


class Structured:
    """Stands in for Claude's structured outputs: each call gives the next scripted answer."""

    def __init__(self, answers: list[Draft | None]) -> None:
        self.answers, self.calls = answers, 0

    def with_structured_output(self, schema: Any, **kwargs: Any) -> "Structured":
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        parsed = self.answers.pop(0)
        raw = AIMessage(
            content=parsed.model_dump_json() if parsed else "not json",
            response_metadata={"model": "claude-opus-5-5", "stop_reason": "end_turn",
                               "usage": {"input_tokens": 100, "output_tokens": 50}},
        )  # fmt: skip
        return {"raw": raw, "parsed": parsed,
                "parsing_error": None if parsed else ValueError("bad")}  # fmt: skip


def run(answers: list[Draft | None]) -> tuple[Any, Structured, MemoryLedger]:
    structured = Structured(answers)
    ledger = MemoryLedger()

    def make(policy: Any, role: str, *, api_key: str, max_tokens: int) -> Structured:
        return structured

    claude = Claude(POLICY, Meter(Budget(tokens=10**6, cost_usd=Decimal(10)), ledger),
                    lambda: "key", make_model=make)  # type: ignore[arg-type]  # fmt: skip
    composed = asyncio.run(compose(
        claude, "Turn the report into the result.", case_id=CASE, snapshot_id=SNAPSHOT,
        report="Report.", seen=seen(),
    ))  # fmt: skip
    return composed, structured, ledger


def test_a_wrong_result_is_repaired_with_its_problems_appended_never_edited() -> None:
    uncited = draft(observed_facts=[{"statement": "Oil is fine", "citations": []}])
    composed, structured, ledger = run([uncited, draft()])
    assert composed.repairs == 1 and composed.draft == draft()
    # Two calls, both in the supervisor's role, both paid for; the history guard let the second
    # through, so the first answer and its problems were appended, not edited.
    assert structured.calls == 2 and [c.role for c in ledger.calls] == ["supervisor"] * 2


def test_an_unparseable_answer_is_repaired_too() -> None:
    composed, structured, _ = run([None, draft()])
    assert composed.repairs == 1 and structured.calls == 2


def test_a_result_still_wrong_after_two_repairs_ends_the_run() -> None:
    uncited = draft(observed_facts=[{"statement": "Oil is fine", "citations": []}])
    with pytest.raises(InvalidResultError, match="after 2 repairs"):
        run([uncited, uncited, uncited])


def test_a_forecast_and_a_summarized_reading_may_be_cited() -> None:
    """M9.0: the forecast an agent read, and each frozen metric's summary, are references."""
    found = Seen()
    found.add("weather_get_forecast", json.dumps({"forecast_id": "fct_" + "a" * 20, "hours": []}))
    metric = {"record_id": "tsnap_" + "b" * 20 + "#gearbox_temperature"}
    summary = {"snapshot_id": "tsnap_" + "b" * 20, "metrics": [metric]}
    found.add("windops_get_telemetry_summary", json.dumps(summary))
    assert "fct_" + "a" * 20 in found.references
    assert "tsnap_" + "b" * 20 + "#gearbox_temperature" in found.references
