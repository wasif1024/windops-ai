"""Step a fleet through time, a second at a time, from a seed (SIM-001).

Each wind farm has one slowly changing wind, which its turbines share, and each turbine has its
own turbulence on top, as in a real array. Every random number comes from a stream seeded by the
simulator's seed and the farm or turbine ID, so the same seed always gives the same data, on any
machine.
"""

import hashlib
import math
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from windops_simulator import models
from windops_simulator.fleet import TurbineSpec

SOURCE = "windops-simulator"
SCHEMA_VERSION = "1"
# Typical site conditions: offshore wind is stronger and smoother than onshore.
MEAN_WIND_MPS = {True: 9.5, False: 7.0}
TURBULENCE_INTENSITY = {True: 0.08, False: 0.14}
MEAN_DRIFT_SECONDS = 7200.0  # how slowly the farm's mean wind wanders
GUST_SECONDS = 8.0  # how quickly turbulence comes and goes
ROTOR_LAG_SECONDS = 6.0  # the rotor's inertia smooths the wind it turns into power
PREVAILING_DIRECTION_DEG = 240.0  # south-westerly, as in north-west Europe


def _stream(seed: int, name: str) -> random.Random:
    """A random stream for one farm or turbine, the same for a seed on every machine."""
    digest = hashlib.sha256(f"{seed}:{name}".encode()).digest()
    # Reproducible on purpose: this simulates weather and wear, and protects nothing.
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311


def _ornstein_uhlenbeck(
    value: float, mean: float, sigma: float, tau: float, dt: float, normal: float
) -> float:
    """A value pulled back toward its mean, with noise: how wind wanders and gusts."""
    decay = math.exp(-dt / tau)
    return mean + (value - mean) * decay + sigma * math.sqrt(1 - decay**2) * normal


@dataclass
class _FarmWind:
    random: random.Random
    site_mean: float
    mean: float
    direction: float

    def step(self, dt: float) -> None:
        self.mean = max(
            0.0,
            _ornstein_uhlenbeck(
                self.mean, self.site_mean, 2.5, MEAN_DRIFT_SECONDS, dt, self.random.gauss(0, 1)
            ),
        )
        self.direction = (self.direction + self.random.gauss(0, 0.05) * math.sqrt(dt)) % 360


@dataclass
class _TurbineState:
    spec: TurbineSpec
    design: models.Design
    random: random.Random
    gust: float = 0.0
    rotor_wind: float = 0.0
    temperatures: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Sample:
    """One second of one turbine's telemetry, with what is needed to reproduce it."""

    tenant_id: str
    turbine_id: str
    event_time: datetime
    wind_speed_mps: float
    wind_direction_deg: float
    rotor_speed_rpm: float
    generator_speed_rpm: float
    power_kw: float
    pitch_angle_deg: float
    ambient_temp_c: float
    gearbox_oil_temp_c: float
    gearbox_bearing_temp_c: float
    main_bearing_temp_c: float
    generator_bearing_temp_c: float
    nacelle_vibration_mm_s: float
    # "producing", "idle_low_wind", "stopped_high_wind", or the Asset status that holds it still.
    operating_state: str
    available: bool
    seed: int
    model_version: str = models.MODEL_VERSION
    schema_version: str = SCHEMA_VERSION
    source: str = SOURCE
    # SIM-006: every sample says what it is.
    synthetic: bool = True

    def as_dict(self) -> dict[str, object]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        values["event_time"] = self.event_time.isoformat()
        return values


_THERMALS = {
    "gearbox_oil": models.GEARBOX_OIL,
    "gearbox_bearing": models.GEARBOX_BEARING,
    "main_bearing": models.MAIN_BEARING,
    "generator_bearing": models.GENERATOR_BEARING,
}


class Simulator:
    """A fleet of turbines, stepped forward in time from a seed."""

    def __init__(self, seed: int, turbines: Sequence[TurbineSpec], start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start needs a time zone, as event times are compared across sites")
        self.seed = seed
        self.now = start
        self._winds: dict[str, _FarmWind] = {}
        self._turbines: list[_TurbineState] = []
        for spec in turbines:
            if spec.wind_farm_id not in self._winds:
                stream = _stream(seed, f"farm:{spec.tenant_id}:{spec.wind_farm_id}")
                site = MEAN_WIND_MPS[spec.offshore]
                self._winds[spec.wind_farm_id] = _FarmWind(
                    random=stream,
                    site_mean=site,
                    mean=max(0.0, site + stream.gauss(0, 2.0)),
                    direction=(PREVAILING_DIRECTION_DEG + stream.gauss(0, 20)) % 360,
                )
            design = models.Design(spec.rated_power_kw, spec.rotor_diameter_m)
            state = _TurbineState(
                spec, design, _stream(seed, f"turbine:{spec.tenant_id}:{spec.turbine_id}")
            )
            state.rotor_wind = self._winds[spec.wind_farm_id].mean
            # Start at the temperatures the current load would settle at, not cold.
            ambient = self._ambient(spec)
            load = models.power_kw(design, state.rotor_wind) / spec.rated_power_kw
            state.temperatures = {
                name: model.target_c(ambient, load) for name, model in _THERMALS.items()
            }
            self._turbines.append(state)

    def _ambient(self, spec: TurbineSpec) -> float:
        day = self.now.timetuple().tm_yday + self.now.hour / 24
        hour = self.now.hour + self.now.minute / 60 + spec.longitude / 15  # local solar time
        return models.ambient_c(spec.latitude, day, hour % 24, offshore=spec.offshore)

    def step(self, seconds: float = 1.0) -> list[Sample]:
        """Advance every turbine, and return one sample each, at the new time."""
        self.now += timedelta(seconds=seconds)
        for wind in self._winds.values():
            wind.step(seconds)
        return [self._advance(state, seconds) for state in self._turbines]

    def run(self, duration: timedelta, interval: float = 1.0) -> Iterator[Sample]:
        """Samples for every turbine, every `interval` seconds, until `duration` has passed."""
        steps = int(duration.total_seconds() / interval)
        for _ in range(steps):
            yield from self.step(interval)

    def _advance(self, state: _TurbineState, dt: float) -> Sample:
        spec, design = state.spec, state.design
        wind = self._winds[spec.wind_farm_id]
        sigma = TURBULENCE_INTENSITY[spec.offshore] * wind.mean
        state.gust = _ornstein_uhlenbeck(
            state.gust, 0.0, sigma, GUST_SECONDS, dt, state.random.gauss(0, 1)
        )
        hub_wind = max(0.0, wind.mean + state.gust)
        lag = math.exp(-dt / ROTOR_LAG_SECONDS)
        state.rotor_wind = hub_wind + (state.rotor_wind - hub_wind) * lag

        if spec.status != "operating":
            state_name, power, rpm, pitch = spec.status, 0.0, 0.0, 90.0
        elif state.rotor_wind >= models.CUT_OUT_MPS:
            state_name, power, rpm, pitch = "stopped_high_wind", 0.0, 0.0, 90.0
        elif state.rotor_wind < models.CUT_IN_MPS:
            state_name, power, rpm, pitch = "idle_low_wind", 0.0, 0.0, 0.0
        else:
            state_name = "producing"
            power = models.power_kw(design, state.rotor_wind)
            rpm = models.rotor_rpm(design, state.rotor_wind)
            pitch = models.pitch_deg(design, state.rotor_wind)

        ambient = self._ambient(spec)
        load = power / spec.rated_power_kw
        for name, model in _THERMALS.items():
            state.temperatures[name] = model.step(
                state.temperatures[name], model.target_c(ambient, load), dt
            )
        noise = state.random.gauss
        vibration = max(0.0, models.vibration_mm_s(design, rpm) * (1 + noise(0, 0.05)))
        return Sample(
            tenant_id=spec.tenant_id,
            turbine_id=spec.turbine_id,
            event_time=self.now,
            wind_speed_mps=round(hub_wind, 2),
            wind_direction_deg=round((wind.direction + noise(0, 2)) % 360, 1),
            rotor_speed_rpm=round(rpm, 2),
            generator_speed_rpm=round(rpm * models.GEARBOX_RATIO, 1),
            power_kw=round(power * (1 + noise(0, 0.005)) if power else 0.0, 1),
            pitch_angle_deg=round(pitch, 2),
            ambient_temp_c=round(ambient, 2),
            # Sensors read to a tenth of a degree, with a little noise.
            gearbox_oil_temp_c=round(state.temperatures["gearbox_oil"] + noise(0, 0.1), 1),
            gearbox_bearing_temp_c=round(state.temperatures["gearbox_bearing"] + noise(0, 0.1), 1),
            main_bearing_temp_c=round(state.temperatures["main_bearing"] + noise(0, 0.1), 1),
            generator_bearing_temp_c=round(
                state.temperatures["generator_bearing"] + noise(0, 0.1), 1
            ),
            nacelle_vibration_mm_s=round(vibration, 3),
            operating_state=state_name,
            available=spec.status == "operating",
            seed=self.seed,
        )
