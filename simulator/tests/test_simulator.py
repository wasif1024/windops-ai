"""The simulator's physics are plausible, its output repeats for a seed, and it says so."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from windops_simulator import DEMO_TURBINES, MODEL_VERSION, Simulator, TurbineSpec, models
from windops_simulator.cli import main

START = datetime(2026, 1, 15, tzinfo=UTC)
OFFSHORE = models.Design(rated_power_kw=8000, rotor_diameter_m=167)
ONE = [spec for spec in DEMO_TURBINES if spec.turbine_id == "dogger-east-t01"]


def run(seed: int, minutes: float, turbines: list[TurbineSpec] = ONE) -> list[dict[str, object]]:
    simulator = Simulator(seed, turbines, START)
    return [sample.as_dict() for sample in simulator.run(timedelta(minutes=minutes))]


def test_the_same_seed_gives_the_same_samples_and_another_seed_others() -> None:
    """SIM-001: reproducible, so an evaluation can replay a case exactly."""
    assert run(1001, 5) == run(1001, 5)
    assert run(1001, 5) != run(1002, 5)


def test_a_turbines_samples_do_not_change_when_others_join_the_fleet() -> None:
    """Each turbine has its own random stream, so adding one does not shift another's data."""
    alone = run(1001, 3)
    with_fleet = [
        row for row in run(1001, 3, list(DEMO_TURBINES)) if row["turbine_id"] == "dogger-east-t01"
    ]
    assert alone == with_fleet


def test_every_sample_says_it_is_synthetic_and_how_it_was_made() -> None:
    """SIM-006, and what reproducing it needs."""
    for row in run(7, 1):
        assert row["synthetic"] is True
        assert (row["model_version"], row["seed"], row["source"]) == (
            MODEL_VERSION,
            7,
            "windops-simulator",
        )


@pytest.mark.parametrize(("wind", "expected"), [(2.0, 0.0), (26.0, 0.0)])
def test_no_power_below_cut_in_or_above_cut_out(wind: float, expected: float) -> None:
    assert models.power_kw(OFFSHORE, wind) == expected
    assert models.rotor_rpm(OFFSHORE, wind) == 0.0


def test_the_power_curve_rises_to_rated_power_and_holds_it() -> None:
    """SIM-003: power follows the wind's cube up to the turbine's rating."""
    powers = [models.power_kw(OFFSHORE, wind) for wind in (4, 6, 8, 10)]
    assert powers == sorted(powers) and powers[0] > 0
    # Doubling the wind gives about eight times the power, below rated.
    assert models.power_kw(OFFSHORE, 8) / models.power_kw(OFFSHORE, 4) == pytest.approx(8)
    assert 10.5 < OFFSHORE.rated_wind_mps < 12.5
    for wind in (13, 18, 24):
        assert models.power_kw(OFFSHORE, wind) == 8000


def test_the_rotor_never_passes_its_tip_speed_limit() -> None:
    speeds = [models.rotor_rpm(OFFSHORE, wind) for wind in range(3, 25)]
    assert max(speeds) == pytest.approx(OFFSHORE.maximum_rotor_rpm)
    # About 10 rpm for a 167 m rotor, as published for turbines this size.
    assert 9 < OFFSHORE.maximum_rotor_rpm < 11


def test_pitch_is_flat_below_rated_and_turns_above_it() -> None:
    assert models.pitch_deg(OFFSHORE, 8) == 0
    assert models.pitch_deg(OFFSHORE, 18) > models.pitch_deg(OFFSHORE, 14) > 0
    assert models.pitch_deg(OFFSHORE, 26) == 90


def test_temperatures_rise_with_load_and_approach_their_target_gradually() -> None:
    oil = models.GEARBOX_OIL
    assert oil.target_c(10, 1.0) > oil.target_c(10, 0.5) > oil.target_c(10, 0.0)
    start, target = 40.0, oil.target_c(10, 1.0)
    after_one_constant = oil.step(start, target, oil.time_constant_s)
    # A first-order model covers about 63% of the gap in one time constant.
    assert after_one_constant - start == pytest.approx(0.632 * (target - start), rel=0.01)


def test_gearbox_oil_is_kept_warm_on_a_freezing_day() -> None:
    assert models.GEARBOX_OIL.target_c(-30, 0.0) >= 20


def test_vibration_rises_with_rotor_speed() -> None:
    low, high = (models.vibration_mm_s(OFFSHORE, rpm) for rpm in (2, 10))
    assert high > low > 0
    assert high < 3.5  # inside the ISO 10816-21 healthy range


def test_a_day_of_fleet_data_stays_in_plausible_ranges() -> None:
    """Every value stays where a healthy turbine's would, for a whole simulated day."""
    simulator = Simulator(1001, list(DEMO_TURBINES), START)
    rated = {spec.turbine_id: spec.rated_power_kw for spec in DEMO_TURBINES}
    seen = set()
    for _ in range(24 * 3600 // 10):
        for sample in simulator.step(10):
            # A healthy fleet on a perfect link sends every reading.
            assert sample.wind_speed_mps is not None
            assert sample.gearbox_oil_temp_c is not None
            assert sample.main_bearing_temp_c is not None
            assert sample.nacelle_vibration_mm_s is not None
            assert 0 <= sample.wind_speed_mps < 40
            assert 0 <= sample.power_kw <= rated[sample.turbine_id] * 1.02
            assert 0 <= sample.rotor_speed_rpm <= 12
            assert -20 < sample.ambient_temp_c < 35
            assert 15 < sample.gearbox_oil_temp_c < 80
            assert 0 < sample.main_bearing_temp_c < 55
            assert 0 <= sample.nacelle_vibration_mm_s < 3.5
            assert 0 <= sample.wind_direction_deg < 360
            seen.add(sample.operating_state)
    assert "producing" in seen


def test_a_turbine_not_operating_in_the_asset_service_stays_still() -> None:
    held = [
        TurbineSpec(
            "north-sea", "dogger-east", "x", 54.8, 2.0, 8000, 167, True, status="maintenance"
        )
    ]
    for row in run(1001, 2, held):
        assert (row["power_kw"], row["rotor_speed_rpm"], row["available"]) == (0.0, 0.0, False)
        assert row["operating_state"] == "maintenance"


def test_a_start_without_a_time_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="time zone"):
        Simulator(1, ONE, datetime(2026, 1, 1))


def test_the_demo_fleet_matches_the_asset_services_demo_seed() -> None:
    """So the simulator makes telemetry for turbines that exist (make asset-seed)."""
    from windops_asset.seed import FARMS
    from windops_asset.seed import OFFSHORE as SEED_OFFSHORE
    from windops_asset.seed import ONSHORE as SEED_ONSHORE

    expected = set()
    for farm in FARMS:
        model = SEED_OFFSHORE if farm.offshore else SEED_ONSHORE
        for number in range(1, farm.turbines + 1):
            expected.add(
                (
                    farm.tenant_id,
                    farm.wind_farm_id,
                    f"{farm.wind_farm_id}-t{number:02d}",
                    round(farm.latitude + 0.009 * (number - 1), 5),
                    farm.longitude,
                    model.rated_power_kw,
                    model.rotor_diameter_m,
                    farm.offshore,
                )
            )
    actual = {
        (
            s.tenant_id,
            s.wind_farm_id,
            s.turbine_id,
            s.latitude,
            s.longitude,
            s.rated_power_kw,
            s.rotor_diameter_m,
            s.offshore,
        )
        for s in DEMO_TURBINES
    }
    assert actual == expected


def test_the_command_prints_repeatable_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    arguments = ["--seed", "5", "--minutes", "1", "--every", "30", "--turbine", "atlas-ridge-t01"]
    assert main(arguments) == 0
    first = capsys.readouterr().out.splitlines()
    assert main(arguments) == 0
    assert capsys.readouterr().out.splitlines() == first
    assert len(first) == 2
    assert json.loads(first[0])["synthetic"] is True
