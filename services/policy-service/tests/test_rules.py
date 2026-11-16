"""Policy rules on their own: what may be written, and exactly how each is judged at its limit."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from windops_policy import rules
from windops_policy.defaults import DEFAULT_RULES

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
WIND = {
    "kind": "limit", "category": "weather_window",
    "parameters": {"input": "weather.max_wind_speed_mps", "above": 12.0,
                   "sites": {"windy-point": 10.0}, "max_age_seconds": 3600,
                   "when": {"input": "proposal.requires_climb", "equals": True}},
    "outcome": "block", "when_missing": "block",
}  # fmt: skip


def rule(definition: dict[str, Any] = WIND) -> rules.Rule:
    return rules.parse("r", 1, "R", definition)


def given(name: str, value: Any, age: timedelta = timedelta(0), state: str = "available") -> Any:
    # Weather is gathered only from the Weather Service (FR-POL-004); the rest from wherever.
    source = "weather" if name.startswith("weather.") else "test"
    return rules.Input(name, value, source, observed_at=NOW - age, state=state)


def judge(definition: dict[str, Any], *inputs: rules.Input, site: str = "dogger-east") -> Any:
    return rules.judge(rule(definition), {i.name: i for i in inputs}, site, NOW)


CLIMB = given("proposal.requires_climb", True)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"when_missing": "pass"}, "a missing input never passes"),
        ({"outcome": "pass"}, "outcome is block or escalate"),
        ({"kind": "model"}, "kind is one of"),
        ({"category": "vibes"}, "category is one of"),
        ({"parameters": {"input": "case.mood", "above": 1}}, "input is one of"),
        ({"parameters": {"input": "proposal.confidence", "above": 1, "below": 0}}, "not both"),
        ({"parameters": {"input": "proposal.confidence", "above": "high"}}, "must be a number"),
        ({"parameters": {"input": "case.status", "above": 1}}, "needs a number input"),
        ({"parameters": {"input": "proposal.confidence", "above": 1, "sites": {"x": "y"}}},
         "must be a number"),
        ({"parameters": {"input": "proposal.confidence", "above": 1, "max_age_seconds": 0}},
         "above 0"),
        ({"parameters": {"input": "proposal.confidence", "above": 1, "extra": 1}},
         "unknown parameters"),
        ({"approved_by": "someone"}, "unknown fields"),
    ],
)  # fmt: skip
def test_a_rule_that_cannot_be_judged_exactly_or_could_pass_on_nothing_is_refused(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(rules.RuleError, match=message):
        rule({**WIND, **change})


def test_a_match_rule_lists_values_of_its_inputs_type() -> None:
    match = {"kind": "match", "category": "risk", "outcome": "escalate", "when_missing": "escalate",
             "parameters": {"input": "case.severity", "in": [3]}}  # fmt: skip
    with pytest.raises(rules.RuleError, match="every value must be a str"):
        rule(match)


def test_every_default_rule_parses_and_none_passes_on_a_missing_input() -> None:
    for rule_id, name, _, definition in DEFAULT_RULES:
        parsed = rules.parse(rule_id, 1, name, definition)
        assert parsed.when_missing in ("block", "escalate")


def test_at_the_limit_passes_and_just_over_it_does_not() -> None:
    at = judge(WIND, CLIMB, given("weather.max_wind_speed_mps", 12.0))
    over = judge(WIND, CLIMB, given("weather.max_wind_speed_mps", 12.01))
    assert (at.outcome, over.outcome) == ("pass", "block")
    assert (at.limit, at.limit_from) == (12.0, "rule")


def test_below_is_strict_too() -> None:
    low = {"kind": "limit", "category": "risk", "outcome": "escalate", "when_missing": "escalate",
           "parameters": {"input": "proposal.confidence", "below": 0.6}}  # fmt: skip
    assert judge(low, given("proposal.confidence", 0.6)).outcome == "pass"
    assert judge(low, given("proposal.confidence", 0.59)).outcome == "escalate"


def test_a_site_has_its_own_limit_and_an_unknown_site_gets_the_strictest() -> None:
    windy = judge(WIND, CLIMB, given("weather.max_wind_speed_mps", 11.0), site="windy-point")
    assert (windy.outcome, windy.limit, windy.limit_from) == ("block", 10.0, "windy-point")
    unknown = judge(WIND, CLIMB, given("weather.max_wind_speed_mps", 11.0), site="")
    assert (unknown.outcome, unknown.limit, unknown.limit_from) == ("block", 10.0, "strictest")


@pytest.mark.parametrize(
    ("inputs", "why"),
    [
        ((), "is missing"),
        ((given("weather.max_wind_speed_mps", None, state="missing"),), "is missing"),
        ((given("weather.max_wind_speed_mps", 5.0, state="unavailable"),), "is unavailable"),
        ((given("weather.max_wind_speed_mps", 5.0, age=timedelta(seconds=3601)),), "is stale"),
        ((given("weather.max_wind_speed_mps", "calm"),), "is not a float"),
    ],
)
def test_a_missing_unavailable_stale_or_wrong_input_fails_closed(inputs: Any, why: str) -> None:
    judged = judge(WIND, CLIMB, *inputs)
    assert judged.outcome == "block" and why in judged.reason


def test_an_input_exactly_at_its_oldest_age_is_still_used() -> None:
    edge = given("weather.max_wind_speed_mps", 5.0, age=timedelta(seconds=3600))
    assert judge(WIND, CLIMB, edge).outcome == "pass"


def test_a_rule_applies_only_when_its_condition_holds_and_when_it_cannot_be_read() -> None:
    ground = judge(WIND, given("proposal.requires_climb", False))
    assert ground.outcome == "not_applicable"
    unknown = judge(WIND, given("proposal.requires_climb", None, state="missing"))
    assert unknown.outcome == "block", "a condition that cannot be read is assumed to hold"


def test_match_in_and_not_in() -> None:
    critical = {"kind": "match", "category": "risk", "outcome": "escalate",
                "when_missing": "escalate",
                "parameters": {"input": "case.severity", "in": ["critical"]}}  # fmt: skip
    assert judge(critical, given("case.severity", "critical")).outcome == "escalate"
    assert judge(critical, given("case.severity", "high")).outcome == "pass"
    active = {**critical, "outcome": "block",
              "parameters": {"input": "case.status", "not_in": ["open"]}}  # fmt: skip
    assert judge(active, given("case.status", "closed")).outcome == "block"
    assert judge(active, given("case.status", "open")).outcome == "pass"


def test_the_result_is_the_worst_outcome() -> None:
    def outcome(name: str) -> rules.Outcome:
        return rules.Outcome(rule(), name, None, "", "")

    assert rules.worst([]) == "pass"
    assert rules.worst([outcome("not_applicable")]) == "pass"
    assert rules.worst([outcome("pass"), outcome("escalate")]) == "escalate"
    assert rules.worst([outcome("escalate"), outcome("block"), outcome("pass")]) == "block"


def test_a_versions_hash_is_its_exact_content() -> None:
    first = rules.content_hash("r", 1, WIND)
    assert first == rules.content_hash("r", 1, dict(reversed(list(WIND.items()))))
    assert first != rules.content_hash("r", 2, WIND)
    assert first != rules.content_hash("r", 1, {**WIND, "outcome": "escalate"})


def test_weather_from_anywhere_but_the_structured_provider_is_never_used() -> None:
    """FR-POL-004: research, a note or a caller's figure cannot feed a safety rule."""
    for source in ("proposal", "tavily", "person"):
        calm = rules.Input("weather.max_wind_speed_mps", 5.0, source, observed_at=NOW)
        judged = judge(WIND, CLIMB, calm)
        assert judged.outcome == "block" and "structured forecast provider" in judged.reason
    forecast = rules.Input("weather.max_wind_speed_mps", 5.0, "weather", observed_at=NOW)
    assert judge(WIND, CLIMB, forecast).outcome == "pass"
