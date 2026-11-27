"""Each problem a result's checks report has a fixed kind, for the Responsible AI dashboard
(SRS 10.3): a label never carries the problem's text, which may quote evidence."""

import pytest
from windops_agent_runtime.team.counted import PROBLEM_KINDS, kind_of


@pytest.mark.parametrize(
    ("problem", "kind"),
    [
        ("The answer did not match the result's schema", "invalid_shape"),
        ("observed_facts[0] cites nothing: cite it, or make it a hypothesis", "uncited_claim"),
        ("hypotheses[1] has no support", "unsupported_hypothesis"),
        ("observed_facts[2] cites references no tool returned: ['x']", "unknown_reference"),
        ("evidence_references holds references no tool returned: ['y']", "unknown_reference"),
        ("conflicting_evidence[0] cites unknown references: ['z']", "unknown_reference"),
        ("conflicting_evidence[0] must name at least two sources", "one_sided_conflict"),
        ("impact figure energy_mwh differs from the estimate: give it exactly",
         "changed_impact"),
        ("case_id and snapshot_id must be case_1 and evs_1", "wrong_identifier_or_range"),
        ("Report observed facts, or say in data_gaps why there are none", "no_findings"),
        ("Something new", "other"),
    ],
)  # fmt: skip
def test_each_problem_has_its_kind(problem: str, kind: str) -> None:
    assert kind_of(problem) == kind


def test_kinds_are_a_fixed_set() -> None:
    assert {kind for _, kind in PROBLEM_KINDS} | {"other"} == {
        "invalid_shape", "uncited_claim", "unsupported_hypothesis", "unknown_reference",
        "one_sided_conflict", "changed_impact", "wrong_identifier_or_range", "no_findings",
        "other",
    }  # fmt: skip
