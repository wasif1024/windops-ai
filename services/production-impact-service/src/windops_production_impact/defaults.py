"""The reference data each tenant starts with, version 1 of each, until an administrator changes it.

The power curves are the two synthetic demo models', tabulated every half metre a second from the
simulator's own physics (its models 1.2.0), so an estimate on the demo fleet matches what its
turbines produce; a test holds the two together. The price and the maintenance costs are
placeholders for review, not market figures: each tenant sets its own, and each site's.
"""

import math
from typing import Any

LIBRARY = "impact-references-1"
MANUFACTURER = "WindOps Synthetic"
# The simulator's constants: air at 15 °C and sea level, its power coefficient and drivetrain
# efficiency, and the cut-in and cut-out speeds.
AIR_DENSITY, POWER_COEFFICIENT, DRIVETRAIN = 1.225, 0.45, 0.94
CUT_IN, CUT_OUT = 3.0, 25.0
# A tabulated curve is within this of what a turbine of the model produces in steady wind.
TOLERANCE_PCT = 5.0


def synthetic_curve(rated_kw: float, rotor_m: float) -> list[list[float]]:
    """[wind m/s, kW] every 0.5 m/s, from cut-in to cut-out, as the simulator models it."""
    area = math.pi * (rotor_m / 2) ** 2
    points = []
    for step in range(int(CUT_IN * 2), int(CUT_OUT * 2)):
        wind = step / 2
        watts = 0.5 * AIR_DENSITY * area * POWER_COEFFICIENT * wind**3 * DRIVETRAIN
        points.append([wind, round(min(watts / 1000, rated_kw), 1)])
    # Still running at the last speed below cut-out; above it the curve, and so the output, is 0.
    points.append([CUT_OUT - 0.01, rated_kw])
    return points


def _curve(model: str, rated_kw: float, rotor_m: float) -> dict[str, Any]:
    return {
        "kind": "power_curve",
        "model": {"manufacturer": MANUFACTURER, "model": model},
        "points": synthetic_curve(rated_kw, rotor_m),
        "tolerance_pct": TOLERANCE_PCT,
    }


# (reference ID, name, definition)
DEFAULT_REFERENCES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("power-curve-sg-8-0-167", "SG-8.0-167 power curve, synthetic",
     _curve("SG-8.0-167", 8000.0, 167.0)),
    ("power-curve-sg-4-5-145", "SG-4.5-145 power curve, synthetic",
     _curve("SG-4.5-145", 4500.0, 145.0)),
    ("energy-price", "Energy price",
     {"kind": "energy_price", "eur_per_mwh": 75.0, "sites": {}}),
    ("maintenance-costs", "Maintenance costs",
     {"kind": "maintenance_costs", "labour_eur_per_hour": 95.0,
      # A crane or service vehicle onshore; a crew transfer vessel offshore.
      "access_eur_per_day": {"onshore": 1800.0, "offshore": 14000.0},
      "actions": {
          "inspection": {"labour_hours": 8.0, "access_days": 1.0},
          "repair": {"labour_hours": 24.0, "access_days": 2.0},
          "replacement": {"labour_hours": 96.0, "access_days": 5.0},
          "adjustment": {"labour_hours": 6.0, "access_days": 1.0},
          "monitoring": {"labour_hours": 2.0, "access_days": 0.0},
      }}),
)  # fmt: skip
