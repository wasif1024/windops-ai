"""Condition monitoring (CMS): accelerometer windows from each turbine's drivetrain (FR-TEL-004).

Each turbine has three accelerometers, on the main bearing, the gearbox and the generator. Every
10 minutes each records a window of acceleration, in g, which the device sends as one CMS batch.
A window is the sum of what a real drivetrain makes:

- a tone at the sensor's shaft speed, which grows with speed;
- on the gearbox, the gear mesh tone, which grows with load;
- broadband noise;
- under a fault: rotor imbalance as a strong 1x tone on the main bearing, growing with the square
  of rotor speed; bearing wear as impacts at the bearing's defect frequency, each ringing at the
  structure's resonance, on the worn bearing's sensor, which raises kurtosis and high-frequency
  energy; failing gearbox cooling as a little more gear mesh.

The drivetrain's kinematics, shaft orders and resonances, are fixed for the synthetic fleet, and
the Telemetry Service uses the same ones to place its spectral bands.
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime

from windops_simulator.faults import ActiveFault
from windops_simulator.models import GEARBOX_RATIO

CMS_SCHEMA_VERSION = "1"
SAMPLE_RATE_HZ = 5120
WINDOW_SAMPLES = 8192
UNIT = "g"


@dataclass(frozen=True)
class Sensor:
    name: str
    # "rotor" for the main shaft, or "generator" for the gearbox's high-speed shaft.
    shaft: str
    # The outer-race defect frequency of the bearing it watches, in orders of its shaft.
    bearing_order: float
    # Gear teeth on the high-speed pinion, whose mesh it hears, if any.
    gear_teeth: int | None
    resonance_hz: float


SENSORS = (
    Sensor("main_bearing", "rotor", 8.1, None, 1800.0),
    Sensor("gearbox", "generator", 4.2, 23, 2200.0),
    Sensor("generator", "generator", 3.6, None, 2000.0),
)
# Which sensor hears each bearing a bearing_wear fault may wear.
BEARING_SENSOR = {
    "main_bearing": "main_bearing",
    "gearbox_bearing": "gearbox",
    "generator_bearing": "generator",
}


def shaft_hz(sensor: Sensor, rotor_rpm: float) -> float:
    speed = rotor_rpm * (GEARBOX_RATIO if sensor.shaft == "generator" else 1.0)
    return speed / 60.0


def rms(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0


def window(
    rng: random.Random,
    sensor: Sensor,
    rotor_rpm: float,
    rpm_fraction: float,
    load: float,
    faults: list[ActiveFault],
    now: datetime,
) -> list[float]:
    """One window of acceleration from one sensor, in g."""
    f = shaft_hz(sensor, rotor_rpm)
    turning = f > 0.05
    tones: list[tuple[float, float, float]] = []  # (frequency, amplitude, phase)
    if turning:
        tones.append((f, 0.004 + 0.02 * rpm_fraction, rng.uniform(0, 2 * math.pi)))
        tones.append((2 * f, 0.002 + 0.006 * rpm_fraction, rng.uniform(0, 2 * math.pi)))
    impacts = 0.0
    for fault in faults:
        profile = fault.spec.profile
        if profile == "imbalance" and sensor.name == "main_bearing" and turning:
            tones.append((f, 0.25 * fault.progress(now) * rpm_fraction**2, 0.0))
        if profile == "bearing_wear" and BEARING_SENSOR.get(fault.spec.affected_component) == (
            sensor.name
        ):
            impacts = max(impacts, fault.wear(now))
    if sensor.gear_teeth and turning:
        mesh = f * sensor.gear_teeth
        extra = sum(
            0.04 * fault.progress(now)
            for fault in faults
            if fault.spec.profile == "gearbox_oil_overheating"
        )
        tones.append((mesh, 0.03 + 0.05 * load + extra, rng.uniform(0, 2 * math.pi)))
        tones.append((2 * mesh, 0.01 + 0.02 * load, rng.uniform(0, 2 * math.pi)))
    defect = sensor.bearing_order * f
    if impacts and turning and defect > 0:
        # The defect frequency itself shows a little, as the impacts modulate the signal.
        tones.append((defect, 0.05 * impacts, rng.uniform(0, 2 * math.pi)))
    noise = 0.01 + 0.01 * load
    period = 1.0 / defect if impacts and turning and defect > 0 else math.inf
    next_impact = rng.uniform(0, period) if period < math.inf else math.inf
    ring = 0.0
    values = []
    dt = 1.0 / SAMPLE_RATE_HZ
    decay = math.exp(-dt / 0.002)
    for n in range(WINDOW_SAMPLES):
        t = n * dt
        if t >= next_impact:
            # A fresh impact, slightly irregular in time, as rollers slip.
            ring += 0.8 * impacts
            next_impact += period * (1 + rng.gauss(0, 0.01))
        value = rng.gauss(0, noise)
        for frequency, amplitude, phase in tones:
            value += amplitude * math.sin(2 * math.pi * frequency * t + phase)
        value += ring * math.sin(2 * math.pi * sensor.resonance_hz * t)
        ring *= decay
        values.append(round(value, 5))
    return values
