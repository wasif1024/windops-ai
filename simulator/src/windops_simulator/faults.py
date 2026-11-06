"""The six starting fault profiles (SIM-002), as changes to the physics or to what a sensor reads.

Each starts at its onset, and grows to its full severity over its ramp. A mechanical fault changes
what the turbine does: heat, vibration. A sensor fault changes only what one sensor reports, while
the machine is fine. The false positive changes the wind, so a healthy turbine runs hot under real
high load, which a good investigation must not call a fault.

Faults draw from their own random streams, so a turbine's data is the same with or without a fault
until it begins, and other turbines are never touched.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# The profiles, the component each affects by default, and the label evaluation expects.
PROFILES = {
    "gearbox_oil_overheating": ("gearbox_oil_system", "gearbox_oil_overheating"),
    "bearing_wear": ("main_bearing", "bearing_wear"),
    "imbalance": ("rotor", "rotor_imbalance"),
    "sensor_drift": ("sensor", "sensor_drift"),
    "sensor_dropout": ("sensor", "sensor_dropout"),
    "false_positive_high_load": ("", "no_fault"),
}
BEARINGS = {"main_bearing", "gearbox_bearing", "generator_bearing"}
# Readings a sensor fault may affect.
CHANNELS = {
    "gearbox_oil_temp_c",
    "gearbox_bearing_temp_c",
    "main_bearing_temp_c",
    "generator_bearing_temp_c",
    "nacelle_vibration_mm_s",
    "wind_speed_mps",
}


@dataclass(frozen=True)
class FaultSpec:
    """One fault on one turbine, from `onset_after` the start, at full strength after `ramp`."""

    profile: str
    turbine_id: str
    onset_after: timedelta
    ramp: timedelta
    severity: float = 1.0
    # The bearing that wears, for bearing_wear.
    component: str = ""
    # The reading that drifts or drops out, for the sensor faults.
    channel: str = "gearbox_oil_temp_c"

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"profile is one of: {', '.join(sorted(PROFILES))}")
        if not 0 < self.severity <= 1:
            raise ValueError("severity is more than 0, and at most 1")
        if self.profile == "bearing_wear" and self.component and self.component not in BEARINGS:
            raise ValueError(f"a worn bearing is one of: {', '.join(sorted(BEARINGS))}")
        if self.profile.startswith("sensor_") and self.channel not in CHANNELS:
            raise ValueError(f"channel is one of: {', '.join(sorted(CHANNELS))}")

    @property
    def affected_component(self) -> str:
        if self.profile == "bearing_wear":
            return self.component or "main_bearing"
        return PROFILES[self.profile][0]

    @property
    def label(self) -> str:
        return PROFILES[self.profile][1]


@dataclass
class ActiveFault:
    """A fault as the simulator applies it, with its own random stream."""

    spec: FaultSpec
    onset: datetime
    random: random.Random
    dropped_out: bool = False
    # How the reading drifts: degrees, or mm/s, per hour at full severity.
    drift_per_hour: float = field(default=2.5)

    def progress(self, now: datetime) -> float:
        """From 0 before the onset, to 1 once the ramp is over, scaled by severity."""
        elapsed = (now - self.onset).total_seconds()
        if elapsed <= 0:
            return 0.0
        ramp = max(self.spec.ramp.total_seconds(), 1.0)
        return min(elapsed / ramp, 1.0) * self.spec.severity

    def wear(self, now: datetime) -> float:
        """Wear accelerates, as a damaged bearing's does: slow at first, then faster."""
        linear = self.progress(now)
        return (math.exp(3 * linear) - 1) / (math.e**3 - 1)

    # --- changes to the physics

    def extra_heat_c(self, part: str, now: datetime, load: float) -> float:
        """Degrees added to a part's thermal target."""
        profile = self.spec.profile
        if profile == "gearbox_oil_overheating":
            # Failing cooling: worse under load, but hot even when idle.
            amount = 28.0 * (0.4 + 0.6 * load) * self.progress(now)
            return {"gearbox_oil": amount, "gearbox_bearing": 0.4 * amount}.get(part, 0.0)
        if profile == "bearing_wear":
            # The parts' names match the bearings', such as main_bearing.
            return 20.0 * self.wear(now) if part == self.spec.affected_component else 0.0
        return 0.0

    def extra_vibration_mm_s(self, now: datetime, rpm_fraction: float) -> float:
        profile = self.spec.profile
        if profile == "bearing_wear":
            # Rising broadband vibration, while the rotor turns.
            return 2.2 * self.wear(now) * min(rpm_fraction * 1.5, 1.0)
        if profile == "imbalance":
            # A mass imbalance's force grows with the square of rotor speed.
            return 2.0 * self.progress(now) * rpm_fraction**2
        if profile == "gearbox_oil_overheating":
            return 0.15 * self.progress(now) * rpm_fraction
        return 0.0

    def wind_floor_mps(self, now: datetime) -> float:
        """For the false positive: a sustained strong wind, so the turbine runs at full load."""
        if self.spec.profile != "false_positive_high_load" or self.progress(now) == 0:
            return 0.0
        return 13.0 + 3.0 * self.progress(now)

    # --- changes to what a sensor reports

    def report(self, channel: str, true_value: float, now: datetime) -> float | None:
        """What the sensor sends for a reading, which may differ from the truth."""
        if not self.spec.profile.startswith("sensor_") or channel != self.spec.channel:
            return true_value
        progress = self.progress(now)
        if progress == 0:
            return true_value
        if self.spec.profile == "sensor_drift":
            hours = (now - self.onset).total_seconds() / 3600
            return true_value + self.drift_per_hour * self.spec.severity * hours
        # Dropout: stretches with no reading, more often as the fault grows.
        if self.dropped_out:
            self.dropped_out = self.random.random() > 1 / 120  # about two minutes each
        else:
            self.dropped_out = self.random.random() < 0.004 * progress
        return None if self.dropped_out else true_value
