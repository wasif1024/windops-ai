"""Policy rules as data, checked before use, and judged exactly as written (FR-POL-001, FR-POL-002).

A rule names one input and the limit it must not cross:

- `limit`: crossed when a number is `above` or `below` the limit. A site, by its wind farm ID,
  may have a limit of its own in `sites` (FR-POL-001).
- `match`: crossed when a value is `in`, or `not_in`, a list.

Crossing it gives the rule's `outcome`, block or escalate. Not crossing it passes. A rule may
apply only `when` another input equals a value, such as only to work at height. When its input
is missing, stale or cannot be reached, the outcome is `when_missing`, which must itself be block
or escalate: no rule can pass on an input it does not have (FR-POL-005).
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

HASH_FORMAT = "windops-policy-rule-v1"
KINDS = ("limit", "match")
CATEGORIES = ("safety", "weather_window", "risk", "business", "evidence", "escalation")
FAILING = ("block", "escalate")
# Worst first: the result is the worst of its rules' outcomes.
ORDER = {"block": 3, "escalate": 2, "pass": 1, "not_applicable": 0}
# Every input a rule may name, and what it holds. Anything else is refused.
INPUTS: dict[str, type] = {
    "proposal.action": str,
    "proposal.requires_climb": bool,
    "proposal.window_hours": float,
    "proposal.estimated_cost_eur": float,
    "proposal.estimated_downtime_hours": float,
    "proposal.confidence": float,
    "case.status": str,
    "case.severity": str,
    "evidence.status": str,
    "evidence.age_hours": float,
    "turbine.status": str,
    "weather.max_wind_speed_mps": float,
    "weather.max_gust_mps": float,
}


class RuleError(ValueError):
    """The definition cannot be evaluated exactly."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    name: str
    kind: str
    category: str
    input: str
    outcome: str
    when_missing: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class Input:
    """One input as gathered: its value, where it came from, and whether it can be used."""

    name: str
    value: Any
    source: str
    source_ref: str = ""
    observed_at: datetime | None = None
    # "available", "missing", "stale" or "unavailable".
    state: str = "available"
    note: str = ""


@dataclass(frozen=True)
class Outcome:
    rule: Rule
    outcome: str
    limit: Any
    limit_from: str
    reason: str


def canonical(definition: Mapping[str, Any]) -> str:
    return json.dumps(definition, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(rule_id: str, version: int, definition: Mapping[str, Any]) -> str:
    text = f"{HASH_FORMAT}|{rule_id}|{version}|{canonical(definition)}"
    return hashlib.sha256(text.encode()).hexdigest()


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise RuleError(f"{what} must be a number")
    return float(value)


def _matches(value: Any, kind: type) -> bool:
    if kind is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, kind)


def _check_limit(p: Mapping[str, Any], kind: type) -> None:
    if kind is not float:
        raise RuleError("a limit rule needs a number input")
    if ("above" in p) == ("below" in p):
        raise RuleError('a limit rule has "above" or "below", not both')
    _number(p.get("above", p.get("below")), "the limit")
    sites = p.get("sites", {})
    if not isinstance(sites, dict):
        raise RuleError('"sites" maps wind farm IDs to limits')
    for site, limit in sites.items():
        if not isinstance(site, str) or not site:
            raise RuleError('"sites" maps wind farm IDs to limits')
        _number(limit, f"the limit for {site}")


def _check_match(p: Mapping[str, Any], kind: type) -> None:
    if ("in" in p) == ("not_in" in p):
        raise RuleError('a match rule has "in" or "not_in", not both')
    values = p.get("in", p.get("not_in"))
    if not isinstance(values, list) or not values:
        raise RuleError("a match rule lists at least one value")
    if not all(_matches(v, kind) for v in values):
        raise RuleError(f"every value must be a {kind.__name__}, as {p['input']} is")


def _check_when(p: Mapping[str, Any]) -> None:
    when = p.get("when")
    if when is None:
        return
    if not isinstance(when, dict) or set(when) != {"input", "equals"}:
        raise RuleError('"when" is {"input": ..., "equals": ...}')
    kind = INPUTS.get(when["input"])
    if kind is None or not _matches(when["equals"], kind):
        raise RuleError('"when" must compare a known input with a value of its type')


def parse(rule_id: str, version: int, name: str, definition: Mapping[str, Any]) -> Rule:
    """A checked rule, from its stored definition."""
    kind, category = definition.get("kind"), definition.get("category")
    if kind not in KINDS:
        raise RuleError(f"kind is one of {', '.join(KINDS)}")
    if category not in CATEGORIES:
        raise RuleError(f"category is one of {', '.join(CATEGORIES)}")
    outcome, when_missing = definition.get("outcome"), definition.get("when_missing")
    if outcome not in FAILING:
        raise RuleError("outcome is block or escalate")
    if when_missing not in FAILING:
        raise RuleError("when_missing is block or escalate: a missing input never passes")
    extra = set(definition) - {"kind", "category", "parameters", "outcome", "when_missing"}
    if extra:
        raise RuleError(f"unknown fields: {', '.join(sorted(extra))}")
    p = definition.get("parameters")
    if not isinstance(p, dict):
        raise RuleError("parameters must be an object")
    input_kind = INPUTS.get(p.get("input", ""))
    if input_kind is None:
        raise RuleError(f"input is one of {', '.join(INPUTS)}")
    allowed = {"input", "when", "max_age_seconds"} | (
        {"above", "below", "sites"} if kind == "limit" else {"in", "not_in"}
    )
    unknown = set(p) - allowed
    if unknown:
        raise RuleError(f"unknown parameters: {', '.join(sorted(unknown))}")
    if kind == "limit":
        _check_limit(p, input_kind)
    else:
        _check_match(p, input_kind)
    _check_when(p)
    if "max_age_seconds" in p and (
        isinstance(p["max_age_seconds"], bool) or not isinstance(p["max_age_seconds"], int)
        or p["max_age_seconds"] <= 0
    ):  # fmt: skip
        raise RuleError("max_age_seconds is a whole number of seconds above 0")
    return Rule(rule_id, version, name, kind, category, p["input"], outcome, when_missing, p)


def _usable(rule: Rule, given: Input | None, now: datetime) -> tuple[bool, str]:
    if given is None:
        return False, f"{rule.input} is missing"
    if given.state != "available" or given.value is None:
        state = "missing" if given.state == "available" else given.state
        return False, f"{rule.input} is {state}" + (f": {given.note}" if given.note else "")
    if not _matches(given.value, INPUTS[rule.input]):
        return False, f"{rule.input} is not a {INPUTS[rule.input].__name__}"
    oldest = rule.parameters.get("max_age_seconds")
    if oldest is not None:
        if given.observed_at is None:
            return False, f"{rule.input} has no time, so its age is unknown"
        age = (now - given.observed_at).total_seconds()
        if age > oldest:
            return False, f"{rule.input} is stale: {age:.0f} s old, at most {oldest} s"
    return True, ""


def judge(rule: Rule, inputs: Mapping[str, Input], site: str, now: datetime) -> Outcome:
    """One rule's outcome, from the inputs it names. Never raises for a missing input."""
    p = rule.parameters
    when = p.get("when")
    if when is not None:
        gate = inputs.get(when["input"])
        # A condition that cannot be read is assumed to hold, so the rule still applies.
        if gate is not None and gate.state == "available" and gate.value != when["equals"]:
            return Outcome(
                rule,
                "not_applicable",
                None,
                "",
                f"Applies only when {when['input']} is {json.dumps(when['equals'])}",
            )
    if rule.kind == "limit":
        sites, own = p.get("sites", {}), p.get("above", p.get("below"))
        if site in sites:
            limit, limit_from = sites[site], site
        elif not site and sites:
            # The site could not be read, so no site's looser limit may apply: the strictest does.
            limit = min(own, *sites.values()) if "above" in p else max(own, *sites.values())
            limit_from = "strictest"
        else:
            limit, limit_from = own, "rule"
    else:
        limit, limit_from = p.get("in", p.get("not_in")), "rule"
    usable, why = _usable(rule, inputs.get(rule.input), now)
    if not usable:
        return Outcome(rule, rule.when_missing, limit, limit_from, why)
    value = inputs[rule.input].value
    if rule.kind == "limit":
        above = "above" in p
        crossed = value > limit if above else value < limit
        word = "above" if above else "below"
        reason = f"{rule.input} is {value:g}, {'' if crossed else 'not '}{word} {limit:g}"
    else:
        listed = value in limit
        crossed = listed if "in" in p else not listed
        reason = (
            f"{rule.input} is {json.dumps(value)}, {'' if listed else 'not '}in {json.dumps(limit)}"
        )
    return Outcome(rule, rule.outcome if crossed else "pass", limit, limit_from, reason)


def worst(outcomes: list[Outcome]) -> str:
    """The result: block if any rule blocks, then escalate, else pass."""
    found = max((ORDER[o.outcome] for o in outcomes), default=ORDER["pass"])
    return next(name for name, rank in ORDER.items() if rank == max(found, ORDER["pass"]))
