"""The strongest frequencies in a vibration signal. Reads JSON on stdin, prints JSON.

Standard library only: the sandbox installs nothing. The mean is removed and a Hann window
applied, the signal is zero-padded to a power of two, and a radix-2 FFT gives its spectrum.
"""

import cmath
import json
import math
import sys


def fft(values):
    n = len(values)
    if n == 1:
        return list(values)
    even, odd = fft(values[0::2]), fft(values[1::2])
    out = [0j] * n
    for k in range(n // 2):
        turn = cmath.exp(-2j * math.pi * k / n) * odd[k]
        out[k], out[k + n // 2] = even[k] + turn, even[k] - turn
    return out


def main():
    given = json.load(sys.stdin)
    rate, samples = float(given["sample_rate_hz"]), [float(v) for v in given["samples"]]
    wanted = int(given.get("peaks", 3))
    count = len(samples)
    mean = sum(samples) / count
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (count - 1)) for i in range(count)]
    size = 1 << (count - 1).bit_length()
    signal = [(v - mean) * w for v, w in zip(samples, window, strict=True)]
    signal += [0.0] * (size - count)
    # Scaled so a sine of amplitude A shows about A, allowing for the window's loss.
    scale = 2 / sum(window)
    power = [abs(c) * scale for c in fft(signal)[: size // 2]]
    local = [
        k for k in range(1, size // 2 - 1) if power[k] > power[k - 1] and power[k] >= power[k + 1]
    ]
    strongest = sorted(local, key=lambda k: power[k], reverse=True)[:wanted]
    print(
        json.dumps(
            {
                "peaks": [
                    {"frequency_hz": round(k * rate / size, 4), "amplitude": round(power[k], 6)}
                    for k in strongest
                ],
                "resolution_hz": round(rate / size, 6),
                "samples": count,
            }
        )
    )


main()
