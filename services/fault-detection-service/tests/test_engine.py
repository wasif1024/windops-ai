"""The rules and the engine: each kind on made-up windows, and the defaults on the simulator."""

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from windops_fault_detection import engine, rules
from windops_fault_detection.defaults import DEFAULT_RULES, LIBRARY
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.scenarios import SCENARIOS
from windops_simulator.simulator import Simulator

START = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)
DEFAULTS = [rules.parse(rule_id, 1, name, d) for rule_id, name, _, d in DEFAULT_RULES]


def rule(
    kind: str, parameters: dict[str, Any], levels: list[Any], rule_id: str = "r"
) -> rules.Rule:
    return rules.parse(
        rule_id, 1, rule_id, {"kind": kind, "parameters": parameters, "levels": levels}
    )


def window(values: Sequence[Mapping[str, float | None]], alarms: Any = ()) -> engine.Window:
    samples = [engine.Sample(START + timedelta(seconds=i), v) for i, v in enumerate(values)]
    return engine.Window("t1", 8000.0, 167.0, samples[-1].at, samples, list(alarms))


def test_a_definition_that_cannot_be_evaluated_exactly_is_refused() -> None:
    good = {"kind": "threshold", "parameters": {"metric": "power_kw", "for_seconds": 60},
            "levels": [{"at": 1, "severity": "low"}]}  # fmt: skip
    rules.parse("ok", 1, "ok", good)
    for broken, says in (
        ({**good, "kind": "guess"}, "kind"),
        ({**good, "parameters": {"metric": "mood", "for_seconds": 60}}, "metric"),
        ({**good, "parameters": {"metric": "power_kw", "for_seconds": 0}}, "for_seconds"),
        ({**good, "levels": []}, "level"),
        ({**good, "levels": [{"at": 1, "severity": "urgent"}]}, "severity"),
        # A higher level must never map to a lower severity.
        ({**good, "levels": [{"at": 1, "severity": "high"}, {"at": 2, "severity": "low"}]},
         "lower severity"),
    ):  # fmt: skip
        with pytest.raises(rules.RuleError, match=says):
            rules.parse("x", 1, "x", broken)


def test_a_versions_hash_is_its_exact_content() -> None:
    _, _, _, definition = DEFAULT_RULES[0]
    first = rules.content_hash("gearbox-oil-residual", 1, definition)
    assert first == rules.content_hash(
        "gearbox-oil-residual", 1, dict(reversed(definition.items()))
    )
    assert first != rules.content_hash("gearbox-oil-residual", 2, definition)
    assert len(first) == 64


def test_a_threshold_must_hold_for_its_whole_period_and_maps_to_its_highest_level() -> None:
    r = rule("threshold", {"metric": "gearbox_oil_temp_c", "for_seconds": 60},
             [{"at": 70, "severity": "high"}, {"at": 80, "severity": "critical"}])  # fmt: skip
    hot: list[dict[str, float | None]] = [{"gearbox_oil_temp_c": 85.0}]
    # One reading below the lowest level breaks the hold; so do too many missing ones.
    assert engine.threshold(r, window(hot * 30 + [{"gearbox_oil_temp_c": 60.0}] + hot * 30)) is None
    assert engine.threshold(r, window(hot * 50 + [{}] * 11)) is None
    found = engine.threshold(r, window([{"gearbox_oil_temp_c": 85.0}] * 30
                                  + [{"gearbox_oil_temp_c": 75.0}] * 40))  # fmt: skip
    assert found is not None and (found.severity, found.amount) == ("high", 75.0)


def test_a_residual_finds_heat_its_load_does_not_explain_and_nothing_when_it_does() -> None:
    r = DEFAULTS[[d[0] for d in DEFAULT_RULES].index("gearbox-oil-residual")]
    # At full load in 10 °C air, the oil's target is 10 + 8 + 45 = 63 °C.
    steady = [{"power_kw": 8000.0, "ambient_temp_c": 10.0, "gearbox_oil_temp_c": 63.0}] * 4300
    assert engine.residual(r, window(steady)) is None
    hot = [{**s, "gearbox_oil_temp_c": 72.0} for s in steady]
    found = engine.residual(r, window(hot))
    assert found is not None and found.severity == "high"
    assert found.amount == pytest.approx(9.0, abs=0.01)


def test_a_correlation_needs_every_alarm_and_the_rule_it_names() -> None:
    r = rule("alarm_correlation",
             {"alarm_codes": ["A_ONE", "A_TWO"], "within_seconds": 600},
             [{"at": 1, "severity": "high"}])  # fmt: skip
    one = engine.AlarmView("A_ONE", "", True, START)
    two = engine.AlarmView("A_TWO", "", False, START, START + timedelta(seconds=100))
    old = engine.AlarmView("A_TWO", "", False, START, START - timedelta(hours=1))
    w = window([{}] * 700, [one, two])
    assert engine.alarm_correlation(r, w, {}) is not None
    assert engine.alarm_correlation(r, window([{}] * 700, [one, old]), {}) is None


def test_a_data_gap_is_a_sensor_sending_nothing_not_samples_that_never_arrived() -> None:
    r = rule("data_gap", {"metric": "main_bearing_temp_c", "window_seconds": 100},
             [{"at": 30, "severity": "medium"}])  # fmt: skip
    values: list[dict[str, float | None]] = [{"main_bearing_temp_c": 30.0}] * 60
    values += [{"main_bearing_temp_c": None}] * 40
    found = engine.data_gap(r, window(values))
    assert found is not None and found.amount == pytest.approx(40.0, abs=1.1)
    # The platform was down for most of the window: nothing arrived, so nothing is said.
    samples = window([{"main_bearing_temp_c": 30.0}] * 100).samples
    arrived = [s for s in samples if s.at > samples[0].at + timedelta(seconds=60)]
    sparse = engine.Window("t1", 8000.0, 167.0, arrived[-1].at, arrived)
    assert engine.data_gap(r, sparse) is None


def run(scenario: str | None, hours: float, at: list[float], seed: int = 1001) -> set[str]:
    """Stream a scenario, or the healthy fleet, at 1 Hz, and evaluate the defaults at each hour."""
    if scenario:
        s = SCENARIOS[scenario]
        simulator = Simulator(s.seed, s.turbines, s.start, s.faults)
        specs = s.turbines
    else:
        simulator, specs = Simulator(seed, DEMO_TURBINES, START), DEMO_TURBINES
    keep = engine.lookback(DEFAULTS)
    buffers: dict[str, deque[engine.Sample]] = {spec.turbine_id: deque() for spec in specs}
    found: set[str] = set()
    for second in range(1, int(hours * 3600) + 1):
        for sample in simulator.step(1.0):
            buffer = buffers[sample.turbine_id]
            buffer.append(engine.Sample(sample.event_time, cast(Any, sample.as_dict())))
            while buffer[0].at < sample.event_time - keep:
                buffer.popleft()
        if second / 3600 in at:
            for spec in specs:
                buffer = buffers[spec.turbine_id]
                w = engine.Window(spec.turbine_id, spec.rated_power_kw, spec.rotor_diameter_m,
                             buffer[-1].at, list(buffer))  # fmt: skip
                found |= {f.rule_id for f in engine.evaluate(DEFAULTS, w)}
    return found


def test_the_default_library_finds_a_failing_gearbox_cooler() -> None:
    assert {"gearbox-oil-residual", "gearbox-bearing-residual"} <= run("gearbox-temp-001", 3, [3])


def test_the_default_library_finds_an_imbalance_by_its_vibration() -> None:
    assert "vibration-residual" in run("imbalance-001", 3, [3])


def test_the_default_library_finds_nothing_on_a_healthy_fleet_or_a_hot_but_healthy_turbine() -> (
    None
):
    """A false positive the high-load scenario sets: hot, because it works hard, and healthy."""
    assert run(None, 2.5, [2.5]) == set()
    assert run("high-load-001", 6, [3, 4, 5, 6]) == set()
    assert LIBRARY == "fault-rules-1"
