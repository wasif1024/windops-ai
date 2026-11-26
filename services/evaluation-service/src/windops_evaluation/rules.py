"""What the service decides, with no database: plans, attempt results, outcomes and comparisons.

Each rule fails closed: an attempt that was not run, was skipped, or ran rehearsed in a job on
Claude never counts as passed (SRS 7.7, EVL-002).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SPLITS = ("train", "validation", "held_out")
MODES = ("rehearse", "claude")
SEVERITIES = ("low", "medium", "high", "critical")
# SRS 10.5's thirteen families.
FAMILIES = (
    "turbine_fault_diagnosis", "gearbox_diagnostics", "vibration_analysis", "weather_impact",
    "production_impact", "maintenance_planning", "spare_parts", "safety_escalation",
    "tool_selection", "rag_grounding", "rbac_and_security", "human_approval", "failure_recovery",
)  # fmt: skip
# Critical safety, security, approval and tool-scope cases run at least 5 times on Claude
# (EVL-002), as scripts/evaluation/scenarios.py holds them to.
REPEATED_FAMILIES = frozenset(
    {"safety_escalation", "rbac_and_security", "human_approval", "tool_selection"}
)
MINIMUM_REPEATS = 5
MAXIMUM_RUNS = 20
SCORE_RESULTS = ("pass", "fail", "not_judged")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,39}")


class RuleError(ValueError):
    """The request breaks one of the rules; its message says which."""


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    family: str
    split: str
    severity: str
    runs_required: int
    definition_json: str


def check_dataset(name: str, version: str, scenarios: Sequence[ScenarioSpec]) -> None:
    if not NAME.fullmatch(name):
        raise RuleError("A dataset name is lowercase letters, digits, dots, dashes or underscores")
    if not VERSION.fullmatch(version):
        raise RuleError("A dataset version is letters, digits, dots, dashes or underscores")
    if not scenarios:
        raise RuleError("A dataset needs at least one scenario")
    seen: set[str] = set()
    for s in scenarios:
        where = s.scenario_id or "a scenario"
        if not NAME.fullmatch(s.scenario_id):
            raise RuleError(f"{where}: its ID must be lowercase letters, digits and dashes")
        if s.scenario_id in seen:
            raise RuleError(f"{where} appears twice")
        seen.add(s.scenario_id)
        if s.family not in FAMILIES:
            raise RuleError(f"{where}: {s.family!r} is not one of SRS 10.5's families")
        if s.split not in SPLITS:
            raise RuleError(f"{where}: the split must be one of {', '.join(SPLITS)} (EVL-006)")
        if s.severity not in SEVERITIES:
            raise RuleError(f"{where}: the severity must be one of {', '.join(SEVERITIES)}")
        if not 1 <= s.runs_required <= MAXIMUM_RUNS:
            raise RuleError(f"{where}: runs_required must be from 1 to {MAXIMUM_RUNS}")
        if s.family in REPEATED_FAMILIES and s.runs_required < MINIMUM_REPEATS:
            raise RuleError(
                f"{where}: a {s.family} scenario runs at least {MINIMUM_REPEATS} times (EVL-002)"
            )
        try:
            written = json.loads(s.definition_json)
        except ValueError as error:
            raise RuleError(f"{where}: its definition is not JSON") from error
        if not isinstance(written, dict) or written.get("case_id") != s.scenario_id:
            raise RuleError(f"{where}: its definition must be an object with its own case_id")


def content_hash(scenarios: Iterable[ScenarioSpec]) -> str:
    """Over every field of every scenario, in scenario_id order, so any change shows."""
    digest = hashlib.sha256()
    for s in sorted(scenarios, key=lambda s: s.scenario_id):
        canonical = json.dumps(json.loads(s.definition_json), sort_keys=True,
                               separators=(",", ":"))  # fmt: skip
        for part in (s.scenario_id, s.family, s.split, s.severity, str(s.runs_required), canonical):
            digest.update(part.encode())
            digest.update(b"\x00")
    return digest.hexdigest()


def runs_for(mode: str, runs_required: int) -> int:
    """A rehearsal's model is scripted, so repeating it repeats it: one run. On Claude, every
    run the scenario asks for (EVL-002)."""
    return 1 if mode == "rehearse" else runs_required


def plan(
    mode: str, scenarios: Sequence[ScenarioSpec], splits: Sequence[str], only: Sequence[str]
) -> list[tuple[str, int]]:
    """Every (scenario, attempt) a job must report, in scenario order."""
    if mode not in MODES:
        raise RuleError(f"The mode must be one of {', '.join(MODES)}")
    unknown = set(splits) - set(SPLITS)
    if unknown:
        raise RuleError(f"No such split: {', '.join(sorted(unknown))}")
    known = {s.scenario_id for s in scenarios}
    missing = set(only) - known
    if missing:
        raise RuleError(f"Not in this dataset: {', '.join(sorted(missing))}")
    chosen = [
        s for s in scenarios
        if (not splits or s.split in splits) and (not only or s.scenario_id in only)
    ]  # fmt: skip
    if not chosen:
        raise RuleError("No scenario in this dataset matches those splits and scenarios")
    return [
        (s.scenario_id, attempt)
        for s in sorted(chosen, key=lambda s: s.scenario_id)
        for attempt in range(1, runs_for(mode, s.runs_required) + 1)
    ]


@dataclass(frozen=True)
class Reported:
    status: str
    reason: str


def attempt_result(
    mode: str, status: str, rehearsed: bool, results: Sequence[tuple[str, str]], reason: str
) -> Reported:
    """An attempt's status from what the runner reported: its scores decide passed or failed.

    `results` is each score's (key, result). A runner reports "skipped" or "error" with a reason
    when it could not run the attempt."""
    if status in ("skipped", "error"):
        if not reason.strip():
            raise RuleError("A skipped attempt, or one in error, needs a reason")
        return Reported(status, reason.strip())
    if status not in ("", "passed", "failed"):
        raise RuleError("An attempt is reported as passed, failed, skipped or error")
    if mode == "claude" and rehearsed:
        # A scripted model's run says nothing of Claude's: never counted (EVL-002).
        return Reported("skipped", "Rehearsed, in a job on Claude")
    if not results:
        raise RuleError("A run attempt needs its scores (EVL-003)")
    keys = [key for key, _ in results]
    if len(set(keys)) != len(keys) or not all(NAME.fullmatch(k) for k in keys):
        raise RuleError("Each score needs its own key, in lowercase")
    for _, result in results:
        if result not in SCORE_RESULTS:
            raise RuleError(f"A score's result is one of {', '.join(SCORE_RESULTS)}")
    failed = [key for key, result in results if result == "fail"]
    worked_out = "failed" if failed else "passed"
    if status and status != worked_out:
        raise RuleError(f"Its scores say it {worked_out}, not {status}")
    return Reported(worked_out, f"Failed: {', '.join(failed)}" if failed else "")


@dataclass(frozen=True)
class Totals:
    planned: int
    passed: int
    failed: int
    incomplete: int


def totals(statuses: Sequence[str]) -> Totals:
    passed = sum(1 for s in statuses if s == "passed")
    failed = sum(1 for s in statuses if s == "failed")
    return Totals(len(statuses), passed, failed, len(statuses) - passed - failed)


def outcome(statuses: Sequence[str]) -> str:
    """passed only when every planned attempt passed; incomplete when any was not run, so a
    skipped evaluation is never counted as passed (SRS 7.7)."""
    counted = totals(statuses)
    if counted.planned == 0 or counted.incomplete:
        return "incomplete"
    return "failed" if counted.failed else "passed"


def splits_key(splits: Iterable[str]) -> str:
    return ",".join(sorted(set(splits)))


@dataclass(frozen=True)
class Change:
    scenario_id: str
    benchmark_passed: int
    benchmark_planned: int
    passed: int
    planned: int
    change: str


def compare(
    benchmark: dict[str, tuple[int, int]], current: dict[str, tuple[int, int]]
) -> list[Change]:
    """Each scenario's (passed, planned) in the benchmark and now. A scenario regressed when it
    passed every attempt in the benchmark and not now; fixed is the reverse."""
    changes: list[Change] = []
    for scenario_id in sorted(current):
        passed, planned = current[scenario_id]
        now_clean = planned > 0 and passed == planned
        if scenario_id not in benchmark:
            changes.append(Change(scenario_id, 0, 0, passed, planned, "new"))
            continue
        was_passed, was_planned = benchmark[scenario_id]
        was_clean = was_planned > 0 and was_passed == was_planned
        change = (
            "regressed" if was_clean and not now_clean
            else "fixed" if now_clean and not was_clean
            else "same"
        )  # fmt: skip
        changes.append(Change(scenario_id, was_passed, was_planned, passed, planned, change))
    return changes
