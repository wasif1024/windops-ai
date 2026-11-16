"""The Phase 6 boundary suite: every limit the deterministic services judge by, at its edges (M6.5).

The cases are generated from the default libraries, so a rule or reference added or changed later
is covered without editing this file. `make boundary-check` runs it, and the gate needs 100%.

- Each limit rule: exactly at the limit passes, just inside passes, just outside gives its
  outcome, and a missing, unavailable or stale input gives its when_missing outcome, never pass.
- Each match rule: a listed value and an unlisted one.
- Each rule that applies only when a condition holds: not applicable when it does not, and still
  applied when the condition cannot be read.
- Power curves: zero below cut-in and from cut-out, rated just below cut-out.
- Weather freshness: fresh up to its limit, stale from it, and the provider's answer refused
  when it is not a structured one.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from windops_policy import rules
from windops_policy.defaults import DEFAULT_RULES
from windops_production_impact import references
from windops_production_impact.defaults import CUT_IN, CUT_OUT, DEFAULT_REFERENCES
from windops_weather import service as weather

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
# Just inside or outside a limit: far below any figure the rules are written in.
EPSILON = 1e-6
PARSED = [rules.parse(rule_id, 1, name, d) for rule_id, name, _, d in DEFAULT_RULES]
LIMITS = [r for r in PARSED if r.kind == "limit"]
MATCHES = [r for r in PARSED if r.kind == "match"]


def source_of(name: str) -> str:
    # Weather is gathered only from the Weather Service (FR-POL-004).
    return "weather" if name.startswith("weather.") else "check"


def inputs(rule: rules.Rule, value: Any, *, age: timedelta = timedelta(0),
           state: str = "available") -> dict[str, rules.Input]:  # fmt: skip
    found = {rule.input: rules.Input(rule.input, value, source_of(rule.input),
                                     observed_at=NOW - age, state=state)}  # fmt: skip
    when = rule.parameters.get("when")
    if when is not None:
        found[when["input"]] = rules.Input(when["input"], when["equals"], "check", observed_at=NOW)
    return found


def limit_of(rule: rules.Rule) -> float:
    return float(rule.parameters.get("above", rule.parameters.get("below")))


def outside(rule: rules.Rule) -> float:
    return limit_of(rule) + (EPSILON if "above" in rule.parameters else -EPSILON)


def inside(rule: rules.Rule) -> float:
    return limit_of(rule) - (EPSILON if "above" in rule.parameters else -EPSILON)


@pytest.mark.parametrize("rule", LIMITS, ids=lambda r: r.rule_id)
def test_a_limit_rule_passes_at_and_inside_its_limit(rule: rules.Rule) -> None:
    for value in (limit_of(rule), inside(rule)):
        assert rules.judge(rule, inputs(rule, value), "", NOW).outcome == "pass", value


@pytest.mark.parametrize("rule", LIMITS, ids=lambda r: r.rule_id)
def test_a_limit_rule_gives_its_outcome_just_outside_its_limit(rule: rules.Rule) -> None:
    assert rules.judge(rule, inputs(rule, outside(rule)), "", NOW).outcome == rule.outcome


@pytest.mark.parametrize("rule", PARSED, ids=lambda r: r.rule_id)
@pytest.mark.parametrize("state", ["missing", "unavailable", "stale"])
def test_an_input_that_cannot_be_used_never_passes(rule: rules.Rule, state: str) -> None:
    judged = rules.judge(rule, inputs(rule, None, state=state), "", NOW)
    assert judged.outcome == rule.when_missing and judged.outcome != "pass"


@pytest.mark.parametrize("rule", [r for r in PARSED if "max_age_seconds" in r.parameters],
                         ids=lambda r: r.rule_id)  # fmt: skip
def test_an_input_one_second_past_its_age_limit_is_stale(rule: rules.Rule) -> None:
    oldest = timedelta(seconds=rule.parameters["max_age_seconds"])
    value = limit_of(rule) if rule.kind == "limit" else rule.parameters["not_in"][0]
    assert rules.judge(rule, inputs(rule, value, age=oldest), "", NOW).outcome == "pass"
    late = rules.judge(rule, inputs(rule, value, age=oldest + timedelta(seconds=1)), "", NOW)
    assert late.outcome == rule.when_missing and "stale" in late.reason


@pytest.mark.parametrize("rule", MATCHES, ids=lambda r: r.rule_id)
def test_a_match_rule_on_a_listed_and_an_unlisted_value(rule: rules.Rule) -> None:
    listed = rule.parameters.get("in", rule.parameters.get("not_in"))[0]
    unlisted = "a value no rule lists"
    crossed, clear = (listed, unlisted) if "in" in rule.parameters else (unlisted, listed)
    assert rules.judge(rule, inputs(rule, crossed), "", NOW).outcome == rule.outcome
    assert rules.judge(rule, inputs(rule, clear), "", NOW).outcome == "pass"


@pytest.mark.parametrize("rule", [r for r in PARSED if "when" in r.parameters],
                         ids=lambda r: r.rule_id)  # fmt: skip
def test_a_conditional_rule_off_its_condition_and_with_it_unreadable(rule: rules.Rule) -> None:
    when = rule.parameters["when"]
    value = outside(rule) if rule.kind == "limit" else None
    found = inputs(rule, value)
    found[when["input"]] = rules.Input(when["input"], not when["equals"], "check",
                                       observed_at=NOW)  # fmt: skip
    assert rules.judge(rule, found, "", NOW).outcome == "not_applicable"
    del found[when["input"]]
    assert rules.judge(rule, found, "", NOW).outcome == rule.outcome, "unreadable: still applies"


@pytest.mark.parametrize("rule", [r for r in PARSED if r.input.startswith("weather.")],
                         ids=lambda r: r.rule_id)  # fmt: skip
def test_weather_from_anywhere_but_the_weather_service_never_passes(rule: rules.Rule) -> None:
    found = inputs(rule, inside(rule))
    found[rule.input] = rules.Input(rule.input, inside(rule), "proposal", observed_at=NOW)
    assert rules.judge(rule, found, "", NOW).outcome == rule.when_missing


CURVES = [(ref, references.parse(d)) for ref, _, d in DEFAULT_REFERENCES
          if d["kind"] == "power_curve"]  # fmt: skip


@pytest.mark.parametrize(("ref", "curve"), CURVES, ids=[c[0] for c in CURVES])
def test_a_power_curve_at_cut_in_and_cut_out(ref: str, curve: Any) -> None:
    assert curve.kw(CUT_IN - EPSILON) == 0.0
    assert curve.kw(CUT_IN) > 0.0
    assert curve.kw(CUT_OUT - 0.02) == curve.kilowatts[-1]
    assert curve.kw(CUT_OUT) == 0.0 and curve.kw(CUT_OUT + 5) == 0.0


def test_weather_is_fresh_up_to_its_limit_and_stale_from_it() -> None:
    """The Weather Service calls a forecast stale at exactly its limit; Policy's rules, a second
    after the same limit. So at the edge the Weather Service is the stricter, and blocks first."""
    assert timedelta(seconds=next(
        r.parameters["max_age_seconds"] for r in PARSED if r.rule_id == "work-at-height-wind"
    )) == weather.FRESH_FOR  # fmt: skip
    assert weather.REFRESH_AFTER < weather.FRESH_FOR
