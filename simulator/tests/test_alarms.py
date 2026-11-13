"""Each turbine's controller: alarms raised and cleared by its rules, never flickering."""

from datetime import UTC, datetime, timedelta
from typing import Any

from windops_simulator.alarms import CODES, RULES_VERSION, SENSOR_LOST, Controller
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.scenarios import SCENARIOS
from windops_simulator.simulator import Simulator

START = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)
HEALTHY = {
    "wind_speed_mps": 9.0, "gearbox_oil_temp_c": 50.0, "gearbox_bearing_temp_c": 55.0,
    "main_bearing_temp_c": 30.0, "generator_bearing_temp_c": 50.0, "nacelle_vibration_mm_s": 1.5,
}  # fmt: skip


def feed(controller: Controller, readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for second, reported in enumerate(readings):
        out += controller.observe(reported, START + timedelta(seconds=second))
    return out


def test_an_alarm_raises_after_ten_seconds_over_and_clears_after_thirty_below() -> None:
    hot = {**HEALTHY, "gearbox_oil_temp_c": 64.0}
    between = {**HEALTHY, "gearbox_oil_temp_c": 61.0}
    controller = Controller("north-sea", "dogger-east-t01")
    # 9 seconds over is not enough; then a reading between the levels neither raises nor clears.
    assert feed(controller, [hot] * 9 + [HEALTHY]) == []
    reports = feed(controller, [hot] * 11 + [between] * 40 + [HEALTHY] * 31)
    assert [(r["code"], r["action"]) for r in reports] == [
        ("GBX_OIL_TEMP_HIGH", "raise"), ("GBX_OIL_TEMP_HIGH", "clear"),
    ]  # fmt: skip
    raised = reports[0]
    assert (raised["severity"], raised["tag"], raised["rules_version"]) == (
        "high", "gearbox_oil_temp_c", RULES_VERSION,
    )  # fmt: skip
    # Over from second 0, so raised at 10; below the clear level from second 51, so cleared at 81.
    assert [datetime.fromisoformat(r["event_time"]) for r in reports] == [
        START + timedelta(seconds=10), START + timedelta(seconds=81),
    ]  # fmt: skip
    assert controller.standing() == []


def test_a_sensor_that_sends_nothing_is_lost_and_neither_raises_nor_clears_its_rule() -> None:
    controller = Controller("north-sea", "hornsea-west-t03")
    silent = {**HEALTHY, "main_bearing_temp_c": None}
    reports = feed(controller, [silent] * 11)
    assert [(r["code"], r["tag"], r["severity"]) for r in reports] == [
        (SENSOR_LOST, "main_bearing_temp_c", "low"),
    ]
    assert controller.standing() == [(SENSOR_LOST, "main_bearing_temp_c")]


def test_a_report_resent_is_the_same_report() -> None:
    first, second = (Controller("north-sea", "dogger-east-t01") for _ in range(2))
    hot = [{**HEALTHY, "nacelle_vibration_mm_s": 3.5}] * 11
    assert feed(first, hot) == feed(second, hot)
    assert set(CODES) >= {"NACELLE_VIBRATION_HIGH", SENSOR_LOST}


def test_the_healthy_fleet_raises_nothing_and_a_gearbox_fault_raises_its_alarm() -> None:
    """The thresholds, against the simulator itself, at 1 Hz as it streams."""
    simulator = Simulator(1001, DEMO_TURBINES, START)
    controllers = {s.turbine_id: Controller(s.tenant_id, s.turbine_id) for s in DEMO_TURBINES}
    healthy: list[Any] = []
    for _ in range(2 * 3600):
        for sample in simulator.step(1.0):
            healthy += controllers[sample.turbine_id].observe(sample.as_dict(), sample.event_time)
    assert healthy == []
    scenario = SCENARIOS["gearbox-temp-001"]
    simulator = Simulator(scenario.seed, scenario.turbines, scenario.start, scenario.faults)
    (spec,) = scenario.turbines
    controller = Controller(spec.tenant_id, spec.turbine_id)
    raised = set()
    for _ in range(int(scenario.duration.total_seconds())):
        for sample in simulator.step(1.0):
            raised |= {
                r["code"] for r in controller.observe(sample.as_dict(), sample.event_time)
                if r["action"] == "raise"
            }  # fmt: skip
    assert {"GBX_OIL_TEMP_HIGH", "GBX_OIL_TEMP_CRITICAL"} <= raised
