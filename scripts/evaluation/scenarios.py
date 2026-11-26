"""The regression scenarios, versioned in evaluations/ (SRS 10.5, M8.12).

Each scenario is a JSON file named by its ID. It names the simulator scenario whose telemetry is
replayed for it, and that scenario's hidden ground truth, which never reaches an agent (SIM-005):
the agents see only the case and what their tools return. Loading checks every file against the
schema, the simulator and the agents' allowlists, so a scenario cannot drift from what it tests.
"""

import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from windops_identity.targets import AGENT_TOOLS
from windops_simulator.scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parents[2] / "evaluations"
# The SRS 10.5 dataset families.
Family = Literal[
    "turbine_fault_diagnosis", "gearbox_diagnostics", "vibration_analysis", "weather_impact",
    "production_impact", "maintenance_planning", "spare_parts", "safety_escalation",
    "tool_selection", "rag_grounding", "rbac_and_security", "human_approval", "failure_recovery",
]  # fmt: skip
Severity = Literal["low", "medium", "high", "critical"]
# EVL-002: critical safety, security and approval cases run at least 5 times each.
REPEATED = {"safety_escalation", "rbac_and_security", "human_approval", "tool_selection"}


class CaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # What a person would write: never the ground truth's own words.
    title: str = Field(min_length=1, max_length=500)
    severity: Severity


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Words the top hypothesis should use, if the diagnosis is right.
    diagnosis_terms: list[str] = Field(min_length=1)
    # True or False when the scenario decides whether a person must look; None when it does not.
    escalation_required: bool | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-z0-9-]+-\d{3}$")
    dataset_version: str
    family: Family
    split: Literal["train", "validation", "held_out"]
    scenario: str
    # "simulator:<scenario ID>": the telemetry replayed for it.
    input_fixture: str = Field(pattern=r"^simulator:[a-z0-9-]+$")
    simulator_seed: int = Field(ge=1)
    ground_truth: str
    # EVL-001's recorded Tavily and weather answers come with the full evaluation (Phase 12).
    recorded_external_sources: str | None = None
    case: CaseInput
    # Tools a good investigation calls, by the name the agents see.
    expected_behavior: list[str] = Field(min_length=1)
    # Actions that must never happen, whatever the model says.
    prohibited_behavior: list[str] = Field(min_length=1)
    expected_outcome: str
    expect: Expect
    severity: Severity
    runs_required: int = Field(ge=1, le=10)
    synthetic: Literal[True]

    @property
    def simulator_scenario(self) -> str:
        return self.input_fixture.removeprefix("simulator:")

    @property
    def replay(self) -> "Replay":
        """The telemetry replayed for it: the simulator scenario, with this scenario's seed."""
        return self.simulator_scenario, self.simulator_seed


def tool_names() -> set[str]:
    """Every tool an agent can see, by the name it sees: "windops_get_case", "sandbox_trend_fit"."""
    names = set()
    for scopes in AGENT_TOOLS.values():
        for scope in scopes:
            parts = scope.split(".")
            names.add("_".join(parts[1:]) if parts[0] == "mcp" else "_".join(parts))
    return names


# A simulator scenario and the seed it is run with. A seed other than the simulator's own is a
# variant: the same fault at the same time, with its own noise and link trouble (M12.2).
Replay = tuple[str, int]


def simulated(scenario: Scenario) -> Any:
    """The simulator scenario this one replays, with its seed."""
    return dataclasses.replace(SCENARIOS[scenario.simulator_scenario],
                               seed=scenario.simulator_seed)  # fmt: skip


class ScenarioError(ValueError):
    """A scenario file is wrong."""


def check(scenario: Scenario, file_name: str) -> None:
    if file_name != f"{scenario.case_id}.json":
        raise ScenarioError(f"{file_name} must be named after its case_id")
    simulated = SCENARIOS.get(scenario.simulator_scenario)
    if simulated is None:
        raise ScenarioError(f"{scenario.case_id}: no simulator scenario {scenario.input_fixture}")
    variants = {s.seed for s in SCENARIOS.values()} - {simulated.seed}
    if scenario.simulator_seed in variants:
        raise ScenarioError(
            f"{scenario.case_id}: seed {scenario.simulator_seed} is another simulator scenario's "
            "own: a variant needs a seed of its own"
        )
    label = next((f.label for f in simulated.faults if f.label != "no_fault"), "no_fault")
    if label != scenario.ground_truth:
        raise ScenarioError(f"{scenario.case_id}: the simulator's ground truth is {label}")
    # SIM-005: the case a person writes must not give the answer away.
    if scenario.ground_truth.replace("_", " ") in scenario.case.title.lower():
        raise ScenarioError(f"{scenario.case_id}: its title gives the ground truth away")
    unknown = set(scenario.expected_behavior) - tool_names()
    if unknown:
        raise ScenarioError(f"{scenario.case_id}: no agent has {sorted(unknown)}")
    if scenario.case.severity != scenario.severity:
        raise ScenarioError(f"{scenario.case_id}: its case and its severity differ")
    if scenario.family in REPEATED and scenario.runs_required < 5:
        raise ScenarioError(f"{scenario.case_id}: {scenario.family} runs at least 5 times")


def check_set(scenarios: Sequence[Scenario]) -> None:
    """Train, validation and held-out scenarios stay apart, so prompts are not tuned to the final
    test (EVL-006): each replay, a simulator scenario with its seed, belongs to one split, and no
    two scenarios share a case title."""
    split_of: dict[Replay, tuple[str, str]] = {}
    titles: dict[str, str] = {}
    for s in scenarios:
        split, first = split_of.setdefault(s.replay, (s.split, s.case_id))
        if split != s.split:
            raise ScenarioError(
                f"{s.case_id} ({s.split}) replays {s.simulator_scenario} with seed "
                f"{s.simulator_seed}, as {first} does in {split}: each replay belongs to one split "
                "(EVL-006). Give one a variant seed"
            )
        title = " ".join(s.case.title.lower().split())
        if title in titles:
            raise ScenarioError(f"{s.case_id}: {titles[title]} has the same case title")
        titles[title] = s.case_id


def load(root: Path = ROOT) -> list[Scenario]:
    """Every scenario, checked, in file name order."""
    found = []
    for path in sorted((root / "scenarios").glob("*.json")):
        scenario = Scenario.model_validate(json.loads(path.read_text()))
        check(scenario, path.name)
        found.append(scenario)
    return found


def dataset(
    name: str = "first-regression", root: Path = ROOT
) -> tuple[dict[str, str], list[Scenario]]:
    """A dataset's manifest, and its scenarios in its order."""
    manifest = json.loads((root / "datasets" / f"{name}.json").read_text())
    by_id = {s.case_id: s for s in load(root)}
    missing = [i for i in manifest["scenarios"] if i not in by_id]
    if missing:
        raise ScenarioError(f"{name} names scenarios that do not exist: {missing}")
    chosen = [by_id[i] for i in manifest["scenarios"]]
    check_set(chosen)
    header = {k: str(v) for k, v in manifest.items() if k not in ("scenarios", "versions")}
    return header, chosen
