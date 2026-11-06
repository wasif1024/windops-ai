"""Faults, data problems and ground truth (SIM-002, SIM-004, SIM-005)."""

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import mean
from typing import Any

import pytest
from windops_simulator import DEMO_TURBINES, Simulator
from windops_simulator.delivery import DataProblems, Link
from windops_simulator.faults import PROFILES, FaultSpec
from windops_simulator.scenarios import SCENARIOS, Scenario, run
from windops_simulator.simulator import random_stream

START = datetime(2026, 1, 15, 6, tzinfo=UTC)
HOUR = timedelta(hours=1)
T01 = tuple(spec for spec in DEMO_TURBINES if spec.turbine_id == "dogger-east-t01")


def hours(
    faults: tuple[FaultSpec, ...] = (), *, seed: int = 1001, n: float = 4
) -> list[dict[str, Any]]:
    simulator = Simulator(seed, T01, START, faults)
    return [sample.as_dict() for sample in simulator.run(timedelta(hours=n), interval=10)]


def late(rows: list[dict[str, Any]], field: str) -> float:
    """The mean of a reading over the last hour."""
    tail = [row[field] for row in rows[-360:] if row[field] is not None]
    return mean(float(value) for value in tail)


def fault(profile: str, **extra: object) -> FaultSpec:
    return FaultSpec(profile, "dogger-east-t01", HOUR, HOUR, **extra)  # type: ignore[arg-type]


def test_there_are_the_six_starting_profiles() -> None:
    """SIM-002."""
    assert set(PROFILES) == {
        "gearbox_oil_overheating", "bearing_wear", "imbalance",
        "sensor_drift", "sensor_dropout", "false_positive_high_load",
    }  # fmt: skip


def test_a_turbines_data_is_unchanged_until_its_fault_begins() -> None:
    """Faults draw from their own streams, so the healthy part of a run matches a healthy run."""
    healthy = hours(n=2)
    faulty = hours((fault("gearbox_oil_overheating"),), n=2)
    before = [row for row in healthy if row["event_time"] < (START + HOUR).isoformat()]
    assert faulty[: len(before)] == before
    assert faulty != healthy


def test_gearbox_oil_overheats_well_beyond_healthy() -> None:
    healthy, faulty = hours(), hours((fault("gearbox_oil_overheating"),))
    assert late(faulty, "gearbox_oil_temp_c") > late(healthy, "gearbox_oil_temp_c") + 12
    assert late(faulty, "gearbox_bearing_temp_c") > late(healthy, "gearbox_bearing_temp_c") + 4


@pytest.mark.parametrize("bearing", ["main_bearing", "gearbox_bearing", "generator_bearing"])
def test_a_worn_bearing_heats_and_vibrates(bearing: str) -> None:
    healthy = hours()
    faulty = hours(
        (FaultSpec("bearing_wear", "dogger-east-t01", HOUR, 2 * HOUR, component=bearing),)
    )
    assert late(faulty, f"{bearing}_temp_c") > late(healthy, f"{bearing}_temp_c") + 8
    assert late(faulty, "nacelle_vibration_mm_s") > late(healthy, "nacelle_vibration_mm_s") + 0.8
    # The other bearings stay as they were.
    for other in {"main_bearing", "gearbox_bearing", "generator_bearing"} - {bearing}:
        assert abs(late(faulty, f"{other}_temp_c") - late(healthy, f"{other}_temp_c")) < 0.5


def test_imbalance_is_vibration_alone() -> None:
    healthy, faulty = hours(), hours((fault("imbalance"),))
    assert late(faulty, "nacelle_vibration_mm_s") > late(healthy, "nacelle_vibration_mm_s") + 0.8
    assert abs(late(faulty, "gearbox_oil_temp_c") - late(healthy, "gearbox_oil_temp_c")) < 0.5


def test_a_drifting_sensor_reports_wrongly_while_the_machine_is_healthy() -> None:
    healthy, faulty = hours(), hours((fault("sensor_drift"),))
    assert late(faulty, "gearbox_oil_temp_c") > late(healthy, "gearbox_oil_temp_c") + 5
    # Nothing physical changed: power and the other temperatures are the same.
    assert late(faulty, "power_kw") == late(healthy, "power_kw")
    assert late(faulty, "gearbox_bearing_temp_c") == late(healthy, "gearbox_bearing_temp_c")


def test_a_dropping_sensor_sends_nothing_in_stretches() -> None:
    rows = hours((fault("sensor_dropout", channel="main_bearing_temp_c"),), n=6)
    after = [row for row in rows if row["event_time"] >= (START + 2 * HOUR).isoformat()]
    missing = [row for row in after if row["main_bearing_temp_c"] is None]
    assert 0 < len(missing) < len(after)
    assert all(row["gearbox_oil_temp_c"] is not None for row in rows)


def test_the_false_positive_runs_hot_under_real_load_with_no_fault() -> None:
    healthy, busy = hours(), hours((fault("false_positive_high_load"),))
    assert late(busy, "power_kw") > 7800  # at its 8 MW rating
    assert late(busy, "gearbox_oil_temp_c") > late(healthy, "gearbox_oil_temp_c")
    # Hot from load alone, within a healthy turbine's range.
    assert late(busy, "gearbox_oil_temp_c") < 70
    assert SCENARIOS["high-load-001"].faults[0].label == "no_fault"


@pytest.mark.parametrize(
    "spec",
    [
        {"profile": "melting", "turbine_id": "t", "onset_after": HOUR, "ramp": HOUR},
        {
            "profile": "imbalance",
            "turbine_id": "t",
            "onset_after": HOUR,
            "ramp": HOUR,
            "severity": 2,
        },
        {
            "profile": "bearing_wear",
            "turbine_id": "t",
            "onset_after": HOUR,
            "ramp": HOUR,
            "component": "tower",
        },
        {
            "profile": "sensor_drift",
            "turbine_id": "t",
            "onset_after": HOUR,
            "ramp": HOUR,
            "channel": "x",
        },
    ],
    ids=["profile", "severity", "bearing", "channel"],
)
def test_an_impossible_fault_is_refused(spec: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FaultSpec(**spec)  # type: ignore[arg-type]


def test_a_fault_on_a_turbine_not_simulated_is_refused() -> None:
    with pytest.raises(ValueError, match="not simulated"):
        Simulator(1, T01, START, (FaultSpec("imbalance", "elsewhere", HOUR, HOUR),))


def payloads(problems: DataProblems, n: int = 2000) -> tuple[list[Any], Link]:
    link = Link(problems, random_stream(1, "test"))
    simulator = Simulator(1, T01, START)
    sent: list[Any] = []
    for sample in simulator.run(timedelta(seconds=n)):
        sent += [delivery.payload for delivery in link.carry(sample.as_dict(), sample.event_time)]
    sent += [delivery.payload for delivery in link.flush(simulator.now)]
    return sent, link


@pytest.mark.parametrize("kind", ["missing", "duplicated", "delayed", "malformed", "out_of_order"])
def test_each_data_problem_is_injected_and_recorded(kind: str) -> None:
    """SIM-004, each recorded for scoring detection (FR-TEL-003)."""
    sent, link = payloads(DataProblems(**{kind: 0.05}))  # type: ignore[arg-type]
    recorded = [problem for problem in link.injected if problem.kind == kind]
    assert 50 < len(recorded) < 160
    assert {problem.kind for problem in link.injected} == {kind}
    sequences = [p["sequence"] for p in sent if isinstance(p, dict) and "sequence" in p]
    if kind == "missing":
        assert len(sent) == 2000 - len(recorded)
    if kind == "duplicated":
        assert len(sequences) - len(set(sequences)) == len(recorded)
    if kind == "malformed":
        broken = [
            p
            for p in sent
            if isinstance(p, str) or "event_time" not in p or isinstance(p.get("power_kw"), str)
        ]
        assert len(broken) == len(recorded)
    if kind == "out_of_order":
        swapped = sum(1 for a, b in pairwise(sequences) if b < a)
        assert swapped == len(recorded)


def test_a_perfect_link_sends_each_sample_once_in_order() -> None:
    sent, link = payloads(DataProblems(), 600)
    assert [p["sequence"] for p in sent] == list(range(1, 601))
    assert not link.injected


def test_rates_that_cannot_happen_are_refused() -> None:
    with pytest.raises(ValueError):
        DataProblems(missing=0.6, duplicated=0.6)


def short(name: str) -> Scenario:
    """A preset, cut to 20 minutes, so a test is quick."""
    preset = SCENARIOS[name]
    return Scenario(
        preset.scenario_id, preset.description, preset.seed, preset.start, timedelta(minutes=20),
        preset.turbines, preset.faults, preset.data_problems, preset.detectable_after,
    )  # fmt: skip


def test_a_scenario_is_sent_in_order_and_repeats_for_its_seed() -> None:
    def lines() -> list[str]:
        deliveries, _ = run(short("data-quality-001"))
        return [delivery.as_line() for delivery in deliveries]

    assert lines() == lines()
    deliveries, _ = run(short("data-quality-001"))
    times = [delivery.sent_at for delivery in deliveries]
    assert times == sorted(times)


def test_ground_truth_names_what_happened_and_the_data_names_none_of_it() -> None:
    """SIM-005: the truth is for evaluation, and nothing in the stream gives it away."""
    deliveries, truth = run(short("gearbox-temp-001"))
    sent = [delivery.as_line() for delivery in deliveries]
    document = truth.as_dict()
    assert document["expected_label"] == "gearbox_oil_overheating"
    assert document["faults"][0]["component"] == "gearbox_oil_system"
    assert document["synthetic"] is True
    for line in sent:
        for secret in ("overheating", "gearbox_oil_system", "label", "profile", "ground_truth"):
            assert secret not in line


def test_the_data_quality_scenario_injects_every_kind_and_counts_them() -> None:
    deliveries, truth = run(short("data-quality-001"))
    sent = list(deliveries)
    counts = truth.as_dict()["data_problems"]["counts"]
    assert set(counts) == {"missing", "duplicated", "delayed", "malformed", "out_of_order"}
    assert truth.as_dict()["expected_label"] == "no_fault"
    assert sent


def test_every_preset_runs_and_names_demo_turbines() -> None:
    demo = {spec.turbine_id for spec in DEMO_TURBINES}
    for name, scenario in SCENARIOS.items():
        assert {spec.turbine_id for spec in scenario.turbines} <= demo, name
        assert scenario.scenario_id == name


def test_the_command_keeps_the_ground_truth_out_of_the_output(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from pathlib import Path

    from windops_simulator.cli import main

    assert main(["--scenario", "gearbox-temp-001"]) == 2  # no --ground-truth
    truth = Path(str(tmp_path)) / "truth.json"
    capsys.readouterr()
    assert main(["--scenario", "sensor-dropout-001", "--ground-truth", str(truth)]) == 0
    output = capsys.readouterr().out
    assert "sensor_dropout" not in output
    assert json.loads(truth.read_text())["expected_label"] == "sensor_dropout"
