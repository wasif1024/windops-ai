"""Fault rules: what each one checks, and the severity each finding maps to (FR-FLT-001, 002, 005).

A rule is data: a kind, its parameters, and its severity levels. Every change is a new version,
and an old version never changes, so a detection can always say exactly which rule found it.
Severity comes only from a rule's own levels. Nothing else, and never a model, can set or raise
the severity of what a rule finds.

The kinds, each an established method from wind-turbine condition monitoring:

- `threshold`: a reading, or its rolling mean, at or above a level for long enough.
- `residual`: a reading above what its normal behaviour model explains, for long enough. The
  `thermal` model is a part's first-order response to its load and the air; the `speed` model is
  vibration's rise with rotor speed. Both take their figures from the turbine's datasheet.
- `rolling_zscore`: a recent mean far from its own baseline, in standard deviations.
- `alarm_correlation`: alarms standing together, or an alarm with another rule's finding.
- `data_gap`: too many samples that arrived without a reading, as a failing sensor sends them.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

KINDS = ("threshold", "residual", "rolling_zscore", "alarm_correlation", "data_gap")
SEVERITIES = ("low", "medium", "high", "critical")
METRICS = (
    "wind_speed_mps", "wind_direction_deg", "rotor_speed_rpm", "generator_speed_rpm", "power_kw",
    "pitch_angle_deg", "ambient_temp_c", "gearbox_oil_temp_c", "gearbox_bearing_temp_c",
    "main_bearing_temp_c", "generator_bearing_temp_c", "nacelle_vibration_mm_s",
)  # fmt: skip
# A day at most: the longest window the Telemetry Service streams.
MAXIMUM_SECONDS = 24 * 3600
HASH_FORMAT = "windops-fault-rule-v1"


class RuleError(ValueError):
    """A rule definition that cannot be used. Its message says which part, and why."""


@dataclass(frozen=True)
class Level:
    """At or beyond `at`, a finding has this severity."""

    at: float
    severity: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    name: str
    kind: str
    parameters: Mapping[str, Any]
    levels: tuple[Level, ...]

    def severity_for(self, amount: float) -> str | None:
        """The highest level `amount` reaches, or None if it reaches none."""
        reached = [level for level in self.levels if amount >= level.at]
        return max(reached, key=lambda level: level.at).severity if reached else None


def _number(parameters: Mapping[str, Any], name: str, low: float, high: float) -> float:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise RuleError(f"{name} must be a number from {low} to {high}")
    return float(value)


def _seconds(parameters: Mapping[str, Any], name: str, low: int = 1) -> int:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= MAXIMUM_SECONDS:
        raise RuleError(f"{name} must be whole seconds from {low} to {MAXIMUM_SECONDS}")
    return value


def _metric(parameters: Mapping[str, Any], name: str = "metric") -> str:
    value = parameters.get(name)
    if value not in METRICS:
        raise RuleError(f"{name} must be one of the telemetry readings, such as gearbox_oil_temp_c")
    return str(value)


def _codes(parameters: Mapping[str, Any]) -> list[str]:
    codes = parameters.get("alarm_codes", [])
    if not isinstance(codes, list) or not all(isinstance(c, str) and c for c in codes):
        raise RuleError("alarm_codes must be a list of alarm codes")
    return codes


def check_parameters(kind: str, parameters: Mapping[str, Any]) -> None:
    """Refuse a definition this engine cannot evaluate exactly as written."""
    if kind == "threshold":
        _metric(parameters)
        _seconds(parameters, "for_seconds")
        if "mean_seconds" in parameters:
            _seconds(parameters, "mean_seconds")
    elif kind == "residual":
        _metric(parameters)
        model = parameters.get("model", "thermal")
        if model == "thermal":
            _number(parameters, "rise_at_idle_c", 0, 150)
            _number(parameters, "rise_at_full_load_c", 0, 200)
            _number(parameters, "time_constant_s", 60, 6 * 3600)
            _number(parameters, "nacelle_above_ambient_c", 0, 30)
        elif model == "speed":
            _number(parameters, "at_standstill", 0, 100)
            _number(parameters, "rise_at_full_speed", 0, 100)
            _number(parameters, "exponent", 0.5, 4)
            _number(parameters, "maximum_tip_speed_mps", 30, 150)
        else:
            raise RuleError('model must be "thermal" or "speed"')
        _seconds(parameters, "for_seconds")
        if "mean_seconds" in parameters:
            _seconds(parameters, "mean_seconds")
    elif kind == "rolling_zscore":
        _metric(parameters)
        recent = _seconds(parameters, "recent_seconds", 60)
        baseline = _seconds(parameters, "baseline_seconds", 600)
        if baseline <= recent:
            raise RuleError("baseline_seconds must be longer than recent_seconds")
        _number(parameters, "minimum_spread", 0, 1000)
    elif kind == "alarm_correlation":
        codes = _codes(parameters)
        also = parameters.get("with_rule", "")
        if not isinstance(also, str):
            raise RuleError("with_rule must be a rule ID")
        if len(codes) + bool(also) < 2:
            raise RuleError("A correlation needs two alarm codes, or an alarm code and with_rule")
        _seconds(parameters, "within_seconds")
    elif kind == "data_gap":
        _metric(parameters)
        _seconds(parameters, "window_seconds", 60)
    else:
        raise RuleError(f"kind must be one of: {', '.join(KINDS)}")


def parse(rule_id: str, version: int, name: str, definition: Mapping[str, Any]) -> Rule:
    """A checked rule, from its stored definition: {"kind", "parameters", "levels"}."""
    kind, parameters = definition.get("kind"), definition.get("parameters")
    if not isinstance(kind, str) or not isinstance(parameters, dict):
        raise RuleError("A rule needs a kind and its parameters")
    check_parameters(kind, parameters)
    raw = definition.get("levels")
    if not isinstance(raw, list) or not raw:
        raise RuleError("A rule needs at least one severity level")
    levels = []
    for item in raw:
        if not isinstance(item, dict) or item.get("severity") not in SEVERITIES:
            raise RuleError(f"Each level needs a severity: {', '.join(SEVERITIES)}")
        at = item.get("at")
        if isinstance(at, bool) or not isinstance(at, int | float):
            raise RuleError("Each level needs a number to be reached, at")
        levels.append(Level(float(at), str(item["severity"])))
    ordered = sorted(levels, key=lambda level: level.at)
    ranks = [SEVERITIES.index(level.severity) for level in ordered]
    if ranks != sorted(ranks) or len({level.at for level in ordered}) != len(ordered):
        raise RuleError("A higher level must not map to a lower severity, and each is distinct")
    return Rule(rule_id, version, name, kind, dict(parameters), tuple(ordered))


def canonical(definition: Mapping[str, Any]) -> str:
    """The definition as one exact text, for its hash: sorted keys, no spaces."""
    return json.dumps(definition, sort_keys=True, separators=(",", ":"))


def content_hash(rule_id: str, version: int, definition: Mapping[str, Any]) -> str:
    """So anyone holding a version can show it is the one a detection used (FR-FLT-003)."""
    text = f"{HASH_FORMAT}|{rule_id}|{version}|{canonical(definition)}"
    return hashlib.sha256(text.encode()).hexdigest()
