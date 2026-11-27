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
from windops_simulator import documents
from windops_simulator.scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parents[2] / "evaluations"
# The SRS 10.5 dataset families.
Family = Literal[
    "turbine_fault_diagnosis", "gearbox_diagnostics", "vibration_analysis", "weather_impact",
    "production_impact", "maintenance_planning", "spare_parts", "safety_escalation",
    "tool_selection", "rag_grounding", "rbac_and_security", "human_approval", "failure_recovery",
]  # fmt: skip
Severity = Literal["low", "medium", "high", "critical"]
# The kinds of action a result may recommend (SRS 5.7).
Action = Literal["inspection", "repair", "replacement", "adjustment", "monitoring"]
# EVL-002: critical safety, security and approval cases run at least 5 times each.
REPEATED = {"safety_escalation", "rbac_and_security", "human_approval", "tool_selection"}
# The tools whose answers carry content from outside the platform: the web, the weather provider
# and uploaded documents. MCP-008: each has a prompt-injection case in the security dataset.
EXTERNAL_TOOLS = frozenset({
    "tavily_search_web", "tavily_extract_page", "tavily_get_research", "weather_get_forecast",
    "documentation_search_documents", "documentation_read_passage",
})  # fmt: skip


class CaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # What a person would write: never the ground truth's own words.
    title: str = Field(min_length=1, max_length=500)
    severity: Severity


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Words the top hypothesis should use, if the diagnosis is right. A term may give its
    # alternatives, "imbalance|unbalance": any one of them will do (M12.3).
    diagnosis_terms: list[str] = Field(min_length=1)
    # True or False when the scenario decides whether a person must look; None when it does not.
    escalation_required: bool | None = None
    # Where the evidence is limited, the most confidence the result may claim (EVL-007's
    # uncertainty); None when the scenario does not decide it.
    max_confidence: float | None = Field(default=None, gt=0, le=1)
    # The kinds of action a good plan leads with, the one a planner drafts. Without repair or
    # replacement among them, as for a healthy turbine, neither may be recommended at all
    # (M12.4). None when the scenario does not decide it.
    actions: list[Action] | None = Field(default=None, min_length=1)
    # True when the result must carry the Production Impact Service's estimate (FR-IMP-004).
    impact_required: bool | None = None
    # True when the result must cite an approved document's passage, a "kn:" citation (M12.5).
    cites_documents: bool | None = None
    # True when the evidence is missing something the result must name as a data gap (M12.5).
    data_gap_required: bool | None = None


class Recording(BaseModel):
    """An external tool's answer, recorded, so a scenario can give it to the agents (EVL-001).
    The text is what the outside source said, before the MCP server wraps it as untrusted. An
    injection names the instruction planted in it, which an agent must treat as data (MCP-008)."""

    model_config = ConfigDict(extra="forbid")
    tool: str
    source: str
    text: str = Field(min_length=1)
    injection: str | None = None


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
    # A recording in evaluations/recordings, by its path there, "injection/tavily-search-web.json":
    # an external tool's answer given to the agents in place of the live source (EVL-001).
    recorded_external_sources: str | None = Field(default=None, pattern=r"^[a-z0-9/-]+\.json$")
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


# The documents only the evaluation tenant's library holds, such as those carrying an injection.
EVALUATION_DOCUMENTS = frozenset(
    d.document_id for d in documents.load() if d.purpose == "evaluation"
)


def recording(scenario: Scenario, root: Path = ROOT) -> Recording | None:
    """The scenario's recorded external answer, if it has one."""
    if scenario.recorded_external_sources is None:
        return None
    path = root / "recordings" / scenario.recorded_external_sources
    if not path.is_file():
        raise ScenarioError(
            f"{scenario.case_id}: no recording {scenario.recorded_external_sources}"
        )
    return Recording.model_validate(json.loads(path.read_text()))


def check(scenario: Scenario, file_name: str, root: Path = ROOT) -> None:
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
    recorded = recording(scenario, root)
    if recorded is not None:
        if recorded.tool not in EXTERNAL_TOOLS:
            raise ScenarioError(f"{scenario.case_id}: {recorded.tool} returns no external content")
        if recorded.tool not in scenario.expected_behavior:
            raise ScenarioError(f"{scenario.case_id}: its recording is for a tool it never expects")
        # A documentation tool's recording is a document in the evaluation tenant's library.
        if recorded.tool.startswith("documentation_") and (
            recorded.source.split(" ")[0] not in EVALUATION_DOCUMENTS
        ):
            raise ScenarioError(
                f"{scenario.case_id}: {recorded.source} is not an evaluation document"
            )


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
        check(scenario, path.name, root)
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
