"""The physical models, versioned together (SIM-003). Change MODEL_VERSION with any of them.

Each is a plain function of its inputs, so it can be tested and reasoned about on its own.
Constants are typical published values for modern three-bladed, geared turbines, not any
manufacturer's: the data is synthetic (SIM-006).
"""

import math
from dataclasses import dataclass

MODEL_VERSION = "1.0.0"

AIR_DENSITY = 1.225  # kg/m³, at 15 °C and sea level
POWER_COEFFICIENT = 0.45  # aerodynamic Cp below rated, under the Betz limit of 0.593
DRIVETRAIN_EFFICIENCY = 0.94  # gearbox, generator and converter together
CUT_IN_MPS = 3.0
CUT_OUT_MPS = 25.0
OPTIMAL_TIP_SPEED_RATIO = 8.0
MAXIMUM_TIP_SPEED_MPS = 90.0  # above this, blade erosion and noise limit the rotor speed
MINIMUM_ROTOR_RPM = 5.0
GEARBOX_RATIO = 100.0


@dataclass(frozen=True)
class Design:
    """What the power and speed models need of a turbine."""

    rated_power_kw: float
    rotor_diameter_m: float

    @property
    def swept_area_m2(self) -> float:
        return math.pi * (self.rotor_diameter_m / 2) ** 2

    @property
    def maximum_rotor_rpm(self) -> float:
        return MAXIMUM_TIP_SPEED_MPS / (self.rotor_diameter_m / 2) * 60 / (2 * math.pi)

    @property
    def rated_wind_mps(self) -> float:
        """The wind speed at which the power curve reaches rated power."""
        watts = self.rated_power_kw * 1000
        return math.pow(
            watts
            / (0.5 * AIR_DENSITY * self.swept_area_m2 * POWER_COEFFICIENT * DRIVETRAIN_EFFICIENCY),
            1 / 3,
        )


def power_kw(design: Design, wind_mps: float) -> float:
    """Electrical output for a steady wind: the cubic law up to rated, then flat, then shut down."""
    if wind_mps < CUT_IN_MPS or wind_mps >= CUT_OUT_MPS:
        return 0.0
    aerodynamic = (
        0.5
        * AIR_DENSITY
        * design.swept_area_m2
        * POWER_COEFFICIENT
        * wind_mps**3
        * DRIVETRAIN_EFFICIENCY
    )
    return min(aerodynamic / 1000, design.rated_power_kw)


def rotor_rpm(design: Design, wind_mps: float) -> float:
    """Rotor speed: the best tip-speed ratio below the speed limit, then held at the limit."""
    if wind_mps < CUT_IN_MPS or wind_mps >= CUT_OUT_MPS:
        return 0.0
    ideal = OPTIMAL_TIP_SPEED_RATIO * wind_mps / (design.rotor_diameter_m / 2) * 60 / (2 * math.pi)
    return min(max(ideal, MINIMUM_ROTOR_RPM), design.maximum_rotor_rpm)


def pitch_deg(design: Design, wind_mps: float) -> float:
    """Blade pitch: flat below rated, then turned out of the wind to hold rated power."""
    if wind_mps >= CUT_OUT_MPS:
        return 90.0  # feathered, stopped
    rated = design.rated_wind_mps
    if wind_mps <= rated:
        return 0.0
    # About 1.5 degrees for each metre per second above rated: a typical pitch schedule.
    return min(1.5 * (wind_mps - rated), 30.0)


# The machinery warms the air inside the nacelle, which is what the parts sit in.
NACELLE_ABOVE_AMBIENT_C = 8.0


@dataclass(frozen=True)
class Thermal:
    """A first-order thermal model: the part's temperature moves toward a target set by load."""

    # Degrees above the nacelle's air at full load, and at no load from friction and heaters.
    rise_at_full_load_c: float
    rise_at_idle_c: float
    # How long it takes to cover about 63% of a step change, in seconds.
    time_constant_s: float
    # A heater keeps it from going colder, as gearbox oil must stay thin enough to lubricate.
    minimum_c: float = -50.0

    def target_c(self, ambient_c: float, load: float) -> float:
        nacelle = ambient_c + NACELLE_ABOVE_AMBIENT_C
        # Losses grow with roughly the square of load, as copper and friction losses do.
        rise = self.rise_at_idle_c + (self.rise_at_full_load_c - self.rise_at_idle_c) * load**2
        return max(nacelle + rise, self.minimum_c)

    def step(self, current_c: float, target_c: float, seconds: float) -> float:
        return target_c + (current_c - target_c) * math.exp(-seconds / self.time_constant_s)


# Typical values for a healthy, geared multi-megawatt turbine in service: gearbox oil about 45 to
# 65 °C, its bearings 50 to 75 °C, the main bearing 25 to 40 °C, the generator bearing 40 to 70 °C.
GEARBOX_OIL = Thermal(
    rise_at_full_load_c=45.0, rise_at_idle_c=25.0, time_constant_s=1800, minimum_c=20.0
)
GEARBOX_BEARING = Thermal(rise_at_full_load_c=55.0, rise_at_idle_c=28.0, time_constant_s=1200)
MAIN_BEARING = Thermal(rise_at_full_load_c=22.0, rise_at_idle_c=10.0, time_constant_s=2400)
GENERATOR_BEARING = Thermal(rise_at_full_load_c=45.0, rise_at_idle_c=22.0, time_constant_s=1500)


def vibration_mm_s(design: Design, rpm: float) -> float:
    """Overall nacelle vibration velocity, RMS: rises with rotor speed, and is small when stopped.

    ISO 10816-21 puts a healthy nacelle under about 3.5 mm/s, which this stays well inside.
    """
    return 0.15 + 1.6 * math.pow(rpm / design.maximum_rotor_rpm, 1.5)


def ambient_c(latitude: float, day_of_year: float, hour_of_day: float, *, offshore: bool) -> float:
    """Air temperature: warmer nearer the equator, warmest in late July and in mid-afternoon.

    The sea holds its heat, so offshore air is milder, and swings less by season and by day.
    """
    mean = 27.0 - 0.4 * abs(latitude) + (3.0 if offshore else 0.0)
    seasonal = (4.0 if offshore else 8.0) * math.cos(2 * math.pi * (day_of_year - 200) / 365)
    daily = (1.0 if offshore else 4.0) * math.cos(2 * math.pi * (hour_of_day - 15) / 24)
    return mean + seasonal + daily
