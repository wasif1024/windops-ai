"""CMS vibration features: time-domain statistics and spectral bands for one window (FR-TEL-004).

Plain Python, with no numerical library: a window is a few thousand values, every 10 minutes, so
an iterative radix-2 FFT is fast enough, and adds nothing to the image to patch. A test holds it
to Parseval's theorem and to known tones.

Bands are placed from the drivetrain's kinematics and the shaft speed sent with each batch, so a
band follows its shaft as the turbine speeds up. FEATURE_VERSION changes with any change here, and
every stored feature row carries it, so an investigation knows how its numbers were made.
"""

import cmath
import math
from dataclasses import dataclass

FEATURE_VERSION = "1"
GEARBOX_RATIO = 100.0
BROADBAND_HZ = (10.0, 1000.0)
HIGH_FROM_HZ = 1000.0
# Below this, a shaft is treated as stopped, and its bands have no value.
TURNING_HZ = 0.05


@dataclass(frozen=True)
class Kinematics:
    """Where to look, for one sensor of the synthetic fleet's drivetrain (kinematics version 1)."""

    shaft: str
    bearing_order: float
    gear_teeth: int | None


# The same drivetrain the simulator models. A test keeps the two the same.
KINEMATICS = {
    "main_bearing": Kinematics("rotor", 8.1, None),
    "gearbox": Kinematics("generator", 4.2, 23),
    "generator": Kinematics("generator", 3.6, None),
}
FEATURES = (
    "rms",
    "peak",
    "crest_factor",
    "kurtosis",
    "band_shaft_rms",
    "band_bearing_rms",
    "band_gear_mesh_rms",
    "band_broadband_rms",
    "band_high_rms",
)


def fft(values: list[float]) -> list[complex]:
    """The discrete Fourier transform, for a length that is a power of two."""
    n = len(values)
    if n == 0 or n & (n - 1):
        raise ValueError("The length must be a power of two")
    bits = n.bit_length() - 1
    out = [complex(values[int(f"{i:0{bits}b}"[::-1], 2)]) if bits else complex(values[0])
           for i in range(n)]  # fmt: skip
    size = 2
    while size <= n:
        half = size // 2
        step = cmath.exp(-2j * math.pi / size)
        for start in range(0, n, size):
            w = 1 + 0j
            for k in range(start, start + half):
                even, odd = out[k], out[k + half] * w
                out[k], out[k + half] = even + odd, even - odd
                w *= step
        size *= 2
    return out


def shaft_hz(sensor: str, rotor_rpm: float, generator_rpm: float) -> float:
    rpm = generator_rpm if KINEMATICS[sensor].shaft == "generator" else rotor_rpm
    return rpm / 60.0


def features(
    values: list[float], sample_rate_hz: float, sensor: str, rotor_rpm: float, generator_rpm: float
) -> dict[str, float | None]:
    """Every feature for one window. A band that does not apply, or a stopped shaft's, is None."""
    n = len(values)
    mean = sum(values) / n
    centred = [v - mean for v in values]
    second = sum(v * v for v in centred) / n
    fourth = sum(v**4 for v in centred) / n
    rms = math.sqrt(second)
    peak = max(abs(v) for v in centred)
    # A Hann window, so a tone between two bins does not spill across the spectrum.
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]
    spectrum = fft([v * w for v, w in zip(centred, hann, strict=True)])
    # One-sided power per bin, scaled so a band's power is its share of the mean square.
    scale = 2.0 / (n * sum(w * w for w in hann))
    power = [abs(spectrum[k]) ** 2 * scale for k in range(n // 2 + 1)]
    resolution = sample_rate_hz / n
    nyquist = sample_rate_hz / 2

    def band(low: float, high: float) -> float:
        first, last = (
            max(1, math.floor(low / resolution)),
            min(n // 2, math.ceil(high / resolution)),
        )
        return math.sqrt(sum(power[first : last + 1])) if last >= first else 0.0

    def around(centre: float, spread: float) -> float:
        half = max(centre * spread, 2 * resolution)
        return band(centre - half, centre + half)

    shaft = shaft_hz(sensor, rotor_rpm, generator_rpm)
    turning = shaft > TURNING_HZ
    kinematics = KINEMATICS[sensor]
    return {
        "rms": rms,
        "peak": peak,
        "crest_factor": peak / rms if rms else None,
        "kurtosis": fourth / (second * second) if second else None,
        "band_shaft_rms": band(max(resolution, 0.5 * shaft), max(2.0, 2.5 * shaft))
        if turning
        else None,
        "band_bearing_rms": around(kinematics.bearing_order * shaft, 0.1) if turning else None,
        "band_gear_mesh_rms": around(kinematics.gear_teeth * shaft, 0.05)
        if turning and kinematics.gear_teeth
        else None,
        "band_broadband_rms": band(*BROADBAND_HZ),
        "band_high_rms": band(HIGH_FROM_HZ, nyquist),
    }
