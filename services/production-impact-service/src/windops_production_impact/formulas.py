"""The impact formulas, version impact-formulas-1: deterministic, with a range for each figure.

From a turbine's observed wind and power over a window, and the reference data:

- observed lost energy: what the power curve says the wind should have produced, less what was
  produced, never below 0;
- observed availability: time producing as a share of time with wind between cut-in and cut-out;
- projected lost energy over the planned downtime, holding the window's mean expected output;
- the availability impact on a 730-hour month;
- lost revenue at the site's energy price, the maintenance cost, and their total.

Each range comes from the anemometer's uncertainty and the power curve's tolerance, taken
together at their worst in each direction.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from windops_production_impact.references import EnergyPrice, MaintenanceCosts, PowerCurve

FORMULA_VERSION = "impact-formulas-1"
WIND_UNCERTAINTY_MPS = 0.5
HOURS_A_MONTH = 730.0
# A sample stands for the time until the next one, up to this: a longer gap is missing data.
LONGEST_STEP_SECONDS = 60.0
# Less than this much usable telemetry, and there is no estimate.
LEAST_SECONDS = 600.0


class NotEnoughDataError(ValueError):
    """The window holds too little telemetry to estimate from."""


@dataclass(frozen=True)
class Sample:
    at: datetime
    wind_mps: float
    power_kw: float


@dataclass(frozen=True)
class Figure:
    name: str
    value: float
    low: float
    high: float
    unit: str


@dataclass
class Estimate:
    figures: list[Figure] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    observed_seconds: float = 0.0
    mean_wind_mps: float = 0.0


def _steps(samples: Sequence[Sample]) -> list[float]:
    gaps = [(b.at - a.at).total_seconds() for a, b in itertools.pairwise(samples)]
    steps = [min(gap, LONGEST_STEP_SECONDS) for gap in gaps]
    # The last sample stands for as long as a typical one.
    typical = sorted(steps)[len(steps) // 2] if steps else 1.0
    return [*steps, min(typical, LONGEST_STEP_SECONDS)]


def _mwh(kw: float, seconds: float) -> float:
    return kw * seconds / 3.6e6


def estimate(
    samples: Sequence[Sample], curve: PowerCurve, price: EnergyPrice, costs: MaintenanceCosts,
    *, action: str, downtime_hours: float, offshore: bool, site: str,
) -> Estimate:  # fmt: skip
    samples = sorted(samples, key=lambda s: s.at)
    steps = _steps(samples)
    seconds = sum(steps)
    if seconds < LEAST_SECONDS:
        raise NotEnoughDataError(
            f"{seconds:.0f} s of wind and power in the window, and at least {LEAST_SECONDS:.0f} s "
            "is needed"
        )
    tolerance = curve.tolerance_pct / 100
    expected = low = high = actual = 0.0
    operating = producing = 0.0
    for sample, step in zip(samples, steps, strict=True):
        kw = curve.kw(sample.wind_mps)
        expected += _mwh(kw, step)
        low += _mwh(curve.kw(sample.wind_mps - WIND_UNCERTAINTY_MPS) * (1 - tolerance), step)
        high += _mwh(curve.kw(sample.wind_mps + WIND_UNCERTAINTY_MPS) * (1 + tolerance), step)
        actual += _mwh(max(sample.power_kw, 0.0), step)
        if kw > 0:
            operating += step
            producing += step if sample.power_kw > 0 else 0.0
    hours = seconds / 3600
    lost = (max(expected - actual, 0.0), max(low - actual, 0.0), max(high - actual, 0.0))
    availability = 100.0 * producing / operating if operating else 100.0
    # MWh an hour, as now, over the downtime.
    rates = (expected / hours, low / hours, high / hours)
    projected = tuple(rate * downtime_hours for rate in rates)
    eur, price_from = price.for_site(site)
    labour, access, maintenance = costs.cost(action, offshore)
    revenue = tuple(mwh * eur for mwh in projected)
    monthly = 100.0 * downtime_hours / HOURS_A_MONTH
    result = Estimate(observed_seconds=seconds,
                      mean_wind_mps=sum(s.wind_mps for s in samples) / len(samples))  # fmt: skip
    result.figures = [
        Figure("observed_lost_energy_mwh", lost[0], min(lost), max(lost), "MWh"),
        Figure("observed_availability_pct", availability, availability, availability, "%"),
        Figure("projected_lost_energy_mwh", projected[0], projected[1], projected[2], "MWh"),
        Figure("availability_impact_pct_points", monthly, monthly, monthly, "percentage points"),
        Figure("lost_revenue_eur", revenue[0], revenue[1], revenue[2], "EUR"),
        Figure("maintenance_cost_eur", maintenance, maintenance, maintenance, "EUR"),
        Figure(
            "total_cost_eur",
            revenue[0] + maintenance,
            revenue[1] + maintenance,
            revenue[2] + maintenance,
            "EUR",
        ),
    ]
    result.assumptions = [
        f"The wind over the {downtime_hours:g} h of downtime stays as observed over the "
        f"{hours:.1f} h window, until the Weather Service's forecasts arrive (M6.3)",
        f"The nacelle anemometer reads within ±{WIND_UNCERTAINTY_MPS:g} m/s",
        f"The {curve.manufacturer} {curve.model} power curve is within ±{curve.tolerance_pct:g}%",
        "Availability is time producing as a share of time with wind between cut-in and cut-out",
        f"The availability impact is over a {HOURS_A_MONTH:g}-hour month",
        f"Energy is priced at {eur:g} EUR/MWh, the {'site' if price_from == site else 'tenant'}'s "
        "price",
        f"Maintenance covers labour ({labour:g} EUR) and {'offshore' if offshore else 'onshore'} "
        f"access ({access:g} EUR); parts are priced once the Inventory Service arrives (Phase 9)",
    ]
    return result
