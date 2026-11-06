"""The turbines to simulate. The demo fleet matches the Asset Service's demo seed, one for one.

A test compares the two, so the simulator always produces telemetry for turbines that exist.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TurbineSpec:
    tenant_id: str
    wind_farm_id: str
    turbine_id: str
    latitude: float
    longitude: float
    rated_power_kw: float
    rotor_diameter_m: float
    offshore: bool
    # From the Asset Service. Anything but "operating" holds the rotor still.
    status: str = "operating"


@dataclass(frozen=True)
class _Farm:
    tenant_id: str
    wind_farm_id: str
    latitude: float
    longitude: float
    offshore: bool
    turbines: int


_FARMS = (
    _Farm("north-sea", "dogger-east", 54.82, 2.07, True, 4),
    _Farm("north-sea", "hornsea-west", 53.91, 1.62, True, 4),
    _Farm("atlas-wind", "atlas-ridge", 42.13, -3.71, False, 5),
)
_OFFSHORE = (8000.0, 167.0)
_ONSHORE = (4500.0, 145.0)


def _demo() -> tuple[TurbineSpec, ...]:
    specs = []
    for farm in _FARMS:
        rated, rotor = _OFFSHORE if farm.offshore else _ONSHORE
        for number in range(1, farm.turbines + 1):
            specs.append(
                TurbineSpec(
                    tenant_id=farm.tenant_id,
                    wind_farm_id=farm.wind_farm_id,
                    turbine_id=f"{farm.wind_farm_id}-t{number:02d}",
                    latitude=round(farm.latitude + 0.009 * (number - 1), 5),
                    longitude=farm.longitude,
                    rated_power_kw=rated,
                    rotor_diameter_m=rotor,
                    offshore=farm.offshore,
                )
            )
    return tuple(specs)


DEMO_TURBINES = _demo()
