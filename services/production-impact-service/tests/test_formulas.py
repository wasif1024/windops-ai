"""The reference data and formulas on their own: exact, with ranges, matched to the simulator."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from windops_production_impact import formulas, references
from windops_production_impact.defaults import DEFAULT_REFERENCES
from windops_simulator.models import Design, power_kw

START = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)
DEFAULTS = {ref: references.parse(d) for ref, _, d in DEFAULT_REFERENCES}
CURVE: Any = DEFAULTS["power-curve-ws-8-0-167"]
PRICE: Any = DEFAULTS["energy-price"]
COSTS: Any = DEFAULTS["maintenance-costs"]


def hour(wind: float, power: float, seconds: int = 3600) -> list[formulas.Sample]:
    return [formulas.Sample(START + timedelta(seconds=i), wind, power) for i in range(seconds)]


def test_the_default_curves_are_the_simulators_physics() -> None:
    for ref, rated, rotor in (("power-curve-ws-8-0-167", 8000.0, 167.0),
                              ("power-curve-ws-4-5-145", 4500.0, 145.0)):  # fmt: skip
        curve = DEFAULTS[ref]
        for wind in (3.0, 5.5, 8.0, 10.25, 12.0, 20.0, 24.5):
            expected = power_kw(Design(rated, rotor), wind)
            assert curve.kw(wind) == pytest.approx(expected, rel=0.03, abs=5), (ref, wind)


def test_a_curve_is_zero_outside_cut_in_and_cut_out_and_linear_between_points() -> None:
    assert CURVE.kw(2.9) == 0.0 and CURVE.kw(25.0) == 0.0 and CURVE.kw(30.0) == 0.0
    assert CURVE.kw(24.9) == 8000.0
    midway = (CURVE.kw(8.0) + CURVE.kw(8.5)) / 2
    assert CURVE.kw(8.25) == pytest.approx(midway)


def test_a_stopped_turbine_loses_what_the_wind_would_have_made() -> None:
    result = formulas.estimate(hour(10.0, 0.0), CURVE, PRICE, COSTS, action="repair",
                               downtime_hours=24, offshore=True, site="dogger-east")  # fmt: skip
    figures = {f.name: f for f in result.figures}
    per_hour = CURVE.kw(10.0) / 1000
    assert figures["observed_lost_energy_mwh"].value == pytest.approx(per_hour)
    assert figures["observed_availability_pct"].value == 0.0
    assert figures["projected_lost_energy_mwh"].value == pytest.approx(per_hour * 24)
    assert figures["lost_revenue_eur"].value == pytest.approx(per_hour * 24 * 75)
    # 24 h of labour at 95 EUR, and 2 vessel days at 14,000 EUR.
    assert figures["maintenance_cost_eur"].value == 24 * 95 + 2 * 14000
    total = figures["total_cost_eur"]
    assert total.low < total.value < total.high
    assert figures["availability_impact_pct_points"].value == pytest.approx(100 * 24 / 730)
    assert any("persist" in a or "stays as observed" in a for a in result.assumptions)


def test_a_running_turbine_has_lost_nothing_and_is_fully_available() -> None:
    result = formulas.estimate(hour(10.0, CURVE.kw(10.0)), CURVE, PRICE, COSTS,
                               action="inspection", downtime_hours=8, offshore=False,
                               site="")  # fmt: skip
    figures = {f.name: f for f in result.figures}
    assert figures["observed_lost_energy_mwh"].value == pytest.approx(0, abs=1e-9)
    assert figures["observed_availability_pct"].value == 100.0
    assert figures["maintenance_cost_eur"].value == 8 * 95 + 1 * 1800


def test_the_range_widens_with_the_anemometer_and_the_curve_tolerance() -> None:
    result = formulas.estimate(hour(8.0, 0.0), CURVE, PRICE, COSTS, action="repair",
                               downtime_hours=10, offshore=True, site="")  # fmt: skip
    projected = next(f for f in result.figures if f.name == "projected_lost_energy_mwh")
    assert projected.low == pytest.approx(CURVE.kw(7.5) * 0.95 * 10 / 1000)
    assert projected.high == pytest.approx(CURVE.kw(8.5) * 1.05 * 10 / 1000)


def test_a_site_price_is_used_for_its_site() -> None:
    priced = references.energy_price({"kind": "energy_price", "eur_per_mwh": 75.0,
                                      "sites": {"dogger-east": 90.0}})  # fmt: skip
    result = formulas.estimate(hour(10.0, 0.0), CURVE, priced, COSTS, action="repair",
                               downtime_hours=1, offshore=True, site="dogger-east")  # fmt: skip
    revenue = next(f for f in result.figures if f.name == "lost_revenue_eur")
    assert revenue.value == pytest.approx(CURVE.kw(10.0) / 1000 * 90)


def test_too_little_telemetry_is_no_estimate_and_a_gap_is_not_counted() -> None:
    with pytest.raises(formulas.NotEnoughDataError):
        formulas.estimate(hour(10.0, 0.0, seconds=300), CURVE, PRICE, COSTS, action="repair",
                          downtime_hours=1, offshore=True, site="")  # fmt: skip
    # Two short runs an hour apart: the hour between is missing data, not 3,600 s of output.
    gap = [*hour(10.0, 0.0, seconds=400),
           *[formulas.Sample(s.at + timedelta(hours=1), s.wind_mps, s.power_kw)
             for s in hour(10.0, 0.0, seconds=400)]]  # fmt: skip
    result = formulas.estimate(gap, CURVE, PRICE, COSTS, action="repair", downtime_hours=1,
                               offshore=True, site="")  # fmt: skip
    assert result.observed_seconds < 900


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ({"kind": "tariff"}, "kind is one of"),
        ({"kind": "energy_price", "eur_per_mwh": -1}, "from 0"),
        ({"kind": "energy_price", "eur_per_mwh": 75, "extra": 1}, "unknown fields"),
        ({"kind": "power_curve", "model": {"manufacturer": "M", "model": "X"},
          "points": [[5, 100], [4, 200]], "tolerance_pct": 5}, "must rise"),
        ({"kind": "power_curve", "model": {"manufacturer": "M", "model": "X"},
          "points": [[5, 100]], "tolerance_pct": 5}, "at least two"),
        ({"kind": "maintenance_costs", "labour_eur_per_hour": 95,
          "access_eur_per_day": {"onshore": 1, "offshore": 2},
          "actions": {"repair": {"labour_hours": 1, "access_days": 1}}}, "exactly"),
    ],
)  # fmt: skip
def test_reference_data_that_cannot_be_used_exactly_is_refused(
    definition: dict[str, Any], message: str
) -> None:
    with pytest.raises(references.ReferenceError, match=message):
        references.parse(definition)


def test_near_cut_out_the_range_still_holds_the_value() -> None:
    """A stronger wind there gives nothing, so the range's ends are not its least and most."""
    for wind in (24.0, 24.6, 24.9, 25.2):
        result = formulas.estimate(hour(wind, 0.0), CURVE, PRICE, COSTS, action="repair",
                                   downtime_hours=4, offshore=True, site="")  # fmt: skip
        for figure in result.figures:
            assert figure.low <= figure.value <= figure.high, (wind, figure)
        forecast = formulas.ForecastWind((wind,) * 4, "open-meteo:fct_x")
        projected = formulas.estimate(hour(10.0, 0.0), CURVE, PRICE, COSTS, action="repair",
                                      downtime_hours=4, offshore=True, site="",
                                      forecast=forecast).figures[2]  # fmt: skip
        assert projected.low <= projected.value <= projected.high, (wind, projected)
