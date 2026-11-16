# CMS Vibration: Interpretation Guide

| Field | Value |
| --- | --- |
| Document | WS-ENG-CMS-001 |
| Revision | 1 |
| Type | engineering |
| Published | 2025-04-03 |
| Issued by | WindOps Synthetic (fictional manufacturer) |
| Applies to | WS-8.0-167, WS-4.5-145 |
| Classification | confidential |

> **Synthetic document.** Written for testing WindOps AI, about a fictional turbine from a fictional manufacturer. It
> describes no real product, and must not be used on real equipment.

## 1. The CMS

Each turbine has three accelerometers on its drivetrain. Each records a window of acceleration, in g, every 10
minutes: 8,192 values at 5,120 Hz, 1.6 seconds. The unit sends the three windows together, with the rotor and
generator speeds at that moment, and keeps a raw window whenever a sensor's RMS is more than twice its usual level.

## 2. The drivetrain's kinematics

Frequencies are given in orders: multiples of a shaft's speed. The generator shaft turns 100 times faster than the
rotor, through the gearbox.

| Sensor | Shaft it watches | Bearing defect frequency | Gear mesh | Structural resonance |
| --- | --- | --- | --- | --- |
| `main_bearing` | Rotor | 8.1 orders, outer race | None | About 1,800 Hz |
| `gearbox` | High-speed shaft | 4.2 orders, outer race | 23 orders, the 23-tooth pinion | About 2,200 Hz |
| `generator` | High-speed shaft | 3.6 orders, outer race | None | About 2,000 Hz |

## 3. A healthy drivetrain

- Tones at once and twice the shaft speed, growing with speed.
- On the gearbox sensor, the gear mesh at 23 orders, growing with load.
- Broadband noise, growing with load.
- Kurtosis about 3: no impacts.

## 4. Fault signatures

| Fault | What changes |
| --- | --- |
| Rotor imbalance | A strong 1x tone on the main bearing sensor, growing with the square of rotor speed |
| Main bearing wear | Impacts at 8.1 orders of the rotor, each ringing the structure near 1,800 Hz. At this slow speed it shows mostly as kurtosis above 4 |
| Gearbox bearing wear | Impacts at 4.2 orders of the high-speed shaft, ringing near 2,200 Hz: high-frequency energy and kurtosis |
| Generator bearing wear | Impacts at 3.6 orders of the high-speed shaft, ringing near 2,000 Hz |
| Hot, thin gearbox oil | A little more gear mesh at 23 orders |
| Sensor faults, and a healthy turbine at high load | Nothing: these show only in the SCADA readings |

## 5. Reading a trend

A single window is a snapshot. Compare the same sensor at the same speed and load over days. A bearing defect grows:
first as kurtosis, then as energy at its defect frequency, then as broadband energy as the damage spreads.

Envelope analysis, demodulating the band around the resonance, shows the defect frequency of an impacting bearing
more clearly than the plain spectrum does.
