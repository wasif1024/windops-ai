"""The Phase 5 gate's fault, through the default rule library and the case rules (M5.7).

The gate streams it live on the cluster, in about 35 minutes. Here the same seeded scenario runs
in seconds, looked at as the detector does, every 5 minutes once its history is in. A change to the
simulator, its seed, the default rules or the case rules that would fail the gate fails here first.
"""

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from windops_fault_detection import engine, rules
from windops_fault_detection.defaults import DEFAULT_RULES
from windops_investigation import case_rules

from scripts.checks.fleet.fault_case import HISTORY, LIVE_AT_MOST, THE_RULES, scenario_simulator

DEFAULTS = [rules.parse(rule_id, 1, name, d) for rule_id, name, _, d in DEFAULT_RULES]
EVERY = timedelta(minutes=5)
# The detector reads up to 30 seconds ago, so samples still arriving are not judged.
SETTLE = timedelta(seconds=30)


def looks() -> list[tuple[timedelta, dict[str, str]]]:
    """What the default rules find at each of the detector's looks, from when it is let go."""
    start = datetime(2026, 11, 14, 8, tzinfo=UTC)
    simulator, spec = scenario_simulator(start)
    keep = engine.lookback(DEFAULTS)
    buffer: deque[engine.Sample] = deque()
    found: list[tuple[timedelta, dict[str, str]]] = []
    end = HISTORY + LIVE_AT_MOST
    for second in range(1, int(end.total_seconds()) + 1):
        for sample in simulator.step(1.0):
            buffer.append(engine.Sample(sample.event_time, cast(Any, sample.as_dict())))
            while buffer[0].at < sample.event_time - keep:
                buffer.popleft()
        into_fault = timedelta(seconds=second) - HISTORY
        if into_fault >= timedelta(0) and into_fault % EVERY == timedelta(0):
            seen = [s for s in buffer if s.at <= start + timedelta(seconds=second) - SETTLE]
            window = engine.Window(spec.turbine_id, spec.rated_power_kw, spec.rotor_diameter_m,
                                   seen[-1].at, seen)  # fmt: skip
            found.append(
                (into_fault, {f.rule_id: f.severity for f in engine.evaluate(DEFAULTS, window)})
            )
    return found


def test_the_default_rules_find_the_gates_fault_in_time_and_nothing_before_it() -> None:
    found = looks()
    assert found[0] == (timedelta(0), {}), "nothing at the first look, as the fault begins"
    every = {rule: severity for _, f in found for rule, severity in f.items()}
    assert set(every) >= THE_RULES
    assert any(case_rules.rank(s) >= case_rules.rank("high") for s in every.values())
    # Both found, at high, then found again by a later look, well inside the gate's time.
    first_with_both = next(at for at, f in found if set(f) >= THE_RULES)
    assert first_with_both + EVERY < LIVE_AT_MOST - timedelta(minutes=5)


def test_everything_found_joins_one_case_for_the_gearbox() -> None:
    every = {rule for _, f in looks() for rule in f}
    assert {case_rules.group_of(rule) for rule in every} == {"gearbox"}
    assert case_rules.opens_a_case("medium")
