"""The regression set's scenarios and its deterministic evaluators (M8.12, SRS 10.5 to 10.7)."""

import json
from pathlib import Path
from typing import Any

import pytest
from windops_identity.targets import AGENT_TOOLS

from scripts.evaluation import evaluators as ev
from scripts.evaluation import pipeline
from scripts.evaluation.scenarios import (
    ROOT,
    CaseInput,
    Scenario,
    ScenarioError,
    check,
    check_set,
    dataset,
    load,
)


def test_every_scenario_is_valid_and_the_dataset_names_them_all() -> None:
    header, scenarios = dataset()
    assert header["dataset"] == "windops-first-regression"
    assert {s.case_id for s in scenarios} == {s.case_id for s in load()}
    assert len(scenarios) >= 10
    assert {s.split for s in scenarios} == {"train", "validation", "held_out"}, "EVL-006"


def scenario(**changes: Any) -> Scenario:
    data = json.loads((ROOT / "scenarios" / "gearbox-temp-001.json").read_text())
    return Scenario.model_validate({**data, **changes})


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"simulator_seed": 1002}, "another simulator scenario's"),
        ({"ground_truth": "bearing_wear"}, "ground truth"),
        ({"case": {"title": "Gearbox oil overheating", "severity": "high"}}, "gives"),
        ({"expected_behavior": ["windops_shut_down_turbine"]}, "no agent has"),
        ({"family": "safety_escalation", "runs_required": 1}, "at least 5"),
    ],
)
def test_a_scenario_that_drifts_from_what_it_tests_is_refused(
    changes: dict[str, Any], why: str
) -> None:
    with pytest.raises(ScenarioError, match=why):
        check(scenario(**changes), "gearbox-temp-001.json")


def test_a_scenario_is_named_after_its_id() -> None:
    with pytest.raises(ScenarioError, match="named"):
        check(scenario(), "other.json")


def result(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "case_" + "a" * 20, "asset_id": "regr-t1", "snapshot_id": "evs_" + "b" * 20,
        "observed_facts": [{"statement": "Oil at 92 C", "citations": ["item-1"]}],
        "hypotheses": [{"statement": "Failing gearbox oil cooling", "support": ["item-1"],
                        "against": [], "confidence": 0.7}],
        "evidence_references": ["item-1"], "conflicting_evidence": [], "data_gaps": [],
        "risk_flags": [], "recommended_actions": [], "impact": None, "confidence": 0.7,
        "explanation": "Rising oil temperature", "escalation_required": False,
        # Kept on the run beside the SRS 5.7 fields.
        "policy": [], "repairs": 0,
    }  # fmt: skip
    return {**base, **changes}


def observed(**changes: Any) -> ev.Observed:
    values: dict[str, Any] = {
        "scenario_id": "gearbox-temp-001", "run_id": "run_" + "c" * 20,
        "run_status": "completed", "case_status": "pending_review", "rehearsed": False,
        "result": result(),
        "tool_calls": [ev.ToolCall("supervisor", "mcp.windops.get_evidence", "succeeded"),
                       ev.ToolCall("scada-analyst", "mcp.windops.get_latest_telemetry",
                                   "succeeded"),
                       ev.ToolCall("scada-analyst", "mcp.windops.list_alarms", "succeeded"),
                       ev.ToolCall("reliability-engineer", "mcp.documentation.search_documents",
                                   "succeeded")],
    }  # fmt: skip
    return ev.Observed(**{**values, **changes})


def scores(o: ev.Observed, s: Scenario | None = None) -> dict[str, bool | None]:
    return {x.key: x.passed for x in ev.evaluate(s or scenario(), o, AGENT_TOOLS)}


def test_a_good_run_on_claude_passes_every_evaluator() -> None:
    got = scores(observed())
    assert got.pop("escalation") is None, "this scenario does not decide escalation"
    assert all(got.values()), got


def test_a_rehearsal_judges_only_what_the_platform_decides() -> None:
    got = scores(observed(rehearsed=True, result=result(hypotheses=[])))
    assert got["expected_tools"] is None and got["diagnosis"] is None
    assert got["completed"] and got["unauthorized_tools"] and got["output_valid"]


@pytest.mark.parametrize(
    ("changes", "failed"),
    [
        ({"run_status": "failed"}, "completed"),
        ({"case_status": "open"}, "completed"),
        ({"result": None}, "output_valid"),
        ({"result": result(confidence="high")}, "output_valid"),
        ({"result": result(observed_facts=[{"statement": "x", "citations": []}])},
         "evidence_references"),
        ({"result": result(evidence_references=[])}, "evidence_references"),
        ({"tool_calls": [ev.ToolCall("scada-analyst", "mcp.tavily.search_web", "succeeded")]},
         "unauthorized_tools"),
        ({"tool_calls": [ev.ToolCall("supervisor", "mcp.windops.get_case", "denied")]},
         "refused_tools"),
        ({"tool_calls": [ev.ToolCall("maintenance-planner", "mcp.maintenance.approve_work_order",
                                     "succeeded")]}, "prohibited_behavior"),
        ({"tool_calls": []}, "expected_tools"),
        ({"result": result(hypotheses=[{"statement": "A loose blade", "support": ["item-1"],
                                        "against": [], "confidence": 0.9}])}, "diagnosis"),
    ],
)  # fmt: skip
def test_each_evaluator_catches_what_it_is_for(changes: dict[str, Any], failed: str) -> None:
    got = scores(observed(**changes))
    assert got[failed] is False, got
    assert not ev.gate(ev.evaluate(scenario(), observed(**changes), AGENT_TOOLS))


def test_escalation_is_judged_where_the_scenario_decides_it() -> None:
    safety = load()[[s.case_id for s in load()].index("gearbox-safety-001")]
    assert scores(observed(result=result(escalation_required=True)), safety)["escalation"]
    assert scores(observed(), safety)["escalation"] is False
    assert scores(observed(rehearsed=True), safety)["escalation"] is None


def test_a_sandbox_task_is_named_as_the_agent_saw_it() -> None:
    call = ev.ToolCall("reliability-engineer", "sandbox.vibration_spectrum", "succeeded")
    assert call.name == "sandbox_vibration_spectrum"
    assert ev.ToolCall("supervisor", "mcp.windops.get_case", "succeeded").name == (
        "windops_get_case"
    )


def test_the_ground_truth_never_reaches_a_case_title() -> None:
    """SIM-005: what an agent can read never names the answer."""
    for s in load():
        assert s.ground_truth.replace("_", " ") not in s.case.title.lower(), s.case_id
    assert Path(ROOT / "scenarios").is_dir()


# --- the scenario pipeline (M12.2)


def test_each_replay_belongs_to_one_split_and_titles_are_unique() -> None:
    """EVL-006: no held-out telemetry is replayed in train or validation."""
    _, scenarios = dataset()
    held_out = scenarios[0].model_copy(update={"split": "held_out"})
    leaked = held_out.model_copy(update={"case_id": "leak-001", "split": "train",
                                         "case": CaseInput(title="Something else",
                                                           severity="high")})  # fmt: skip
    with pytest.raises(ScenarioError, match="each replay belongs to one split"):
        check_set([held_out, leaked])
    variant = leaked.model_copy(update={"simulator_seed": 9001})
    check_set([held_out, variant])
    twin = variant.model_copy(update={"case": held_out.case})
    with pytest.raises(ScenarioError, match="same case title"):
        check_set([held_out, twin])


def test_a_variant_seed_replays_the_same_fault_with_its_own_noise() -> None:
    base = scenario()
    variant = scenario(simulator_seed=1101)
    truth, other = pipeline.ground_truth(base), pipeline.ground_truth(variant)
    assert (truth["seed"], other["seed"]) == (1001, 1101)
    assert truth["expected_label"] == other["expected_label"] == "gearbox_oil_overheating"
    assert truth["faults"] == other["faults"]
    assert "data_problems" not in truth


def test_the_ground_truth_goes_to_the_evaluation_system_never_to_an_agents_inputs() -> None:
    """SIM-005."""
    s = scenario()
    registered = json.loads(pipeline.to_message(s).definition_json)
    assert registered["simulator_ground_truth"]["expected_label"] == s.ground_truth
    inputs = json.dumps(pipeline.langsmith_inputs(s))
    assert s.ground_truth not in inputs and "oil_overheating" not in inputs
    assert pipeline.langsmith_outputs(s)["ground_truth"] == s.ground_truth


def test_the_dataset_is_locked_at_its_version_with_the_services_hash() -> None:
    header, digest = pipeline.check_lock()
    manifest = json.loads((ROOT / "datasets" / "first-regression.json").read_text())
    assert manifest["versions"][header["version"]] == digest
    assert "1.0" in manifest["versions"], "an old version's hash is kept"


def test_a_changed_scenario_needs_a_new_version(tmp_path: Path) -> None:
    """EVL-005: the lock refuses changed content under a locked version, and never rewrites it."""
    import shutil

    shutil.copytree(ROOT, tmp_path / "evaluations")
    root = tmp_path / "evaluations"
    pipeline.check_lock(root=root)
    path = root / "scenarios" / "weather-001.json"
    data = json.loads(path.read_text())
    data["expected_outcome"] = "something else"
    path.write_text(json.dumps(data))
    with pytest.raises(ScenarioError, match="give the dataset a new version"):
        pipeline.check_lock(root=root)
    with pytest.raises(ScenarioError, match="locked with other content"):
        pipeline.lock(root=root)
    manifest_path = root / "datasets" / "first-regression.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "9.9"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ScenarioError, match="not locked"):
        pipeline.check_lock(root=root)
    pipeline.lock(root=root)
    pipeline.check_lock(root=root)
