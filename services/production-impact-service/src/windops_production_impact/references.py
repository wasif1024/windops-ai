"""The reference data every estimate rests on, checked before use (FR-IMP-001, FR-IMP-002).

- `power_curve`: a turbine model's output by wind speed, as points to interpolate between, with
  its tolerance. Below the first point and above the last, the output is 0.
- `energy_price`: euros a megawatt hour, with a price of its own for any site.
- `maintenance_costs`: the labour rate, the access day rate onshore and offshore, and for each
  action the labour hours and access days it takes.
"""

import bisect
import hashlib
import itertools
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

HASH_FORMAT = "windops-impact-reference-v1"
KINDS = ("power_curve", "energy_price", "maintenance_costs")
ACTIONS = ("inspection", "repair", "replacement", "adjustment", "monitoring")


class ReferenceError(ValueError):
    """The definition cannot be used exactly."""


def canonical(definition: Mapping[str, Any]) -> str:
    return json.dumps(definition, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(reference_id: str, version: int, definition: Mapping[str, Any]) -> str:
    text = f"{HASH_FORMAT}|{reference_id}|{version}|{canonical(definition)}"
    return hashlib.sha256(text.encode()).hexdigest()


def _number(value: Any, what: str, low: float = 0.0, high: float = 1e12) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ReferenceError(f"{what} must be a number")
    if not low <= value <= high:
        raise ReferenceError(f"{what} is from {low:g} to {high:g}")
    return float(value)


def _keys(definition: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(definition) - allowed
    if extra:
        raise ReferenceError(f"unknown fields: {', '.join(sorted(extra))}")
    missing = allowed - set(definition) - {"sites"}
    if missing:
        raise ReferenceError(f"missing fields: {', '.join(sorted(missing))}")


@dataclass(frozen=True)
class PowerCurve:
    manufacturer: str
    model: str
    winds: tuple[float, ...]
    kilowatts: tuple[float, ...]
    tolerance_pct: float

    def kw(self, wind: float) -> float:
        """Linear between points; 0 outside them, below cut-in and above cut-out."""
        if not self.winds or wind < self.winds[0] or wind > self.winds[-1]:
            return 0.0
        index = bisect.bisect_right(self.winds, wind)
        if index >= len(self.winds):
            return self.kilowatts[-1]
        w0, w1 = self.winds[index - 1], self.winds[index]
        k0, k1 = self.kilowatts[index - 1], self.kilowatts[index]
        return k0 + (k1 - k0) * (wind - w0) / (w1 - w0)


def power_curve(definition: Mapping[str, Any]) -> PowerCurve:
    _keys(definition, {"kind", "model", "points", "tolerance_pct"})
    model = definition["model"]
    if (
        not isinstance(model, dict)
        or set(model) != {"manufacturer", "model"}
        or not all(isinstance(v, str) and v for v in model.values())
    ):
        raise ReferenceError('model is {"manufacturer": ..., "model": ...}')
    points = definition["points"]
    if not isinstance(points, list) or len(points) < 2:
        raise ReferenceError("points lists at least two [wind m/s, kW] pairs")
    winds, kws = [], []
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ReferenceError("each point is [wind m/s, kW]")
        winds.append(_number(point[0], "a wind speed", 0, 60))
        kws.append(_number(point[1], "an output", 0, 50000))
    if any(b <= a for a, b in itertools.pairwise(winds)):
        raise ReferenceError("wind speeds must rise from point to point")
    tolerance = _number(definition["tolerance_pct"], "tolerance_pct", 0, 50)
    return PowerCurve(model["manufacturer"], model["model"], tuple(winds), tuple(kws), tolerance)


@dataclass(frozen=True)
class EnergyPrice:
    eur_per_mwh: float
    sites: Mapping[str, float]

    def for_site(self, site: str) -> tuple[float, str]:
        """The site's own price, or the tenant's; "" when the site cannot be read."""
        if site in self.sites:
            return self.sites[site], site
        return self.eur_per_mwh, "tenant"


def energy_price(definition: Mapping[str, Any]) -> EnergyPrice:
    _keys(definition, {"kind", "eur_per_mwh", "sites"})
    price = _number(definition["eur_per_mwh"], "eur_per_mwh", 0, 100000)
    sites = definition.get("sites", {})
    if not isinstance(sites, dict):
        raise ReferenceError('"sites" maps wind farm IDs to prices')
    return EnergyPrice(price, {
        str(site): _number(value, f"the price for {site}", 0, 100000)
        for site, value in sites.items()
    })  # fmt: skip


@dataclass(frozen=True)
class MaintenanceCosts:
    labour_eur_per_hour: float
    access_onshore: float
    access_offshore: float
    actions: Mapping[str, tuple[float, float]]

    def cost(self, action: str, offshore: bool) -> tuple[float, float, float]:
        """Labour, access and their total for one action."""
        hours, days = self.actions[action]
        labour = hours * self.labour_eur_per_hour
        access = days * (self.access_offshore if offshore else self.access_onshore)
        return labour, access, labour + access


def maintenance_costs(definition: Mapping[str, Any]) -> MaintenanceCosts:
    _keys(definition, {"kind", "labour_eur_per_hour", "access_eur_per_day", "actions"})
    access = definition["access_eur_per_day"]
    if not isinstance(access, dict) or set(access) != {"onshore", "offshore"}:
        raise ReferenceError('access_eur_per_day is {"onshore": ..., "offshore": ...}')
    actions = definition["actions"]
    if not isinstance(actions, dict) or set(actions) != set(ACTIONS):
        raise ReferenceError(f"actions has exactly: {', '.join(ACTIONS)}")
    parsed = {}
    for action, figures in actions.items():
        if not isinstance(figures, dict) or set(figures) != {"labour_hours", "access_days"}:
            raise ReferenceError(f'{action} is {{"labour_hours": ..., "access_days": ...}}')
        parsed[action] = (
            _number(figures["labour_hours"], f"{action}'s labour_hours", 0, 10000),
            _number(figures["access_days"], f"{action}'s access_days", 0, 365),
        )
    return MaintenanceCosts(
        _number(definition["labour_eur_per_hour"], "labour_eur_per_hour", 0, 100000),
        _number(access["onshore"], "the onshore access rate"),
        _number(access["offshore"], "the offshore access rate"),
        parsed,
    )


PARSERS = {"power_curve": power_curve, "energy_price": energy_price,
           "maintenance_costs": maintenance_costs}  # fmt: skip


def parse(definition: Mapping[str, Any]) -> Any:
    kind = definition.get("kind")
    if kind not in KINDS:
        raise ReferenceError(f"kind is one of {', '.join(KINDS)}")
    return PARSERS[str(kind)](definition)
