---
name: vibration-analysis
description: How to interpret wind turbine drivetrain vibration, and when a spectrum analysis would help.
version: 2
---

# Vibration analysis

## Reading RMS vibration

- Compare RMS velocity with the turbine's own history at similar rotor speed and power, not with a fixed number.
- A step change matters more than a slow drift. Note when it began, and what else changed then.
- Rising vibration with rising bearing temperature points at a bearing; rising vibration with steady temperatures may
  be imbalance or misalignment.

## When a spectrum would help

- RMS cannot tell a bearing defect from gear mesh damage. A frequency spectrum of the raw vibration can.
- Spectrum analysis runs only in the Agent Sandbox, on the bounded, sanitized snapshot data (SRS 5.9). Its result is
  supporting analysis, and never replaces a deterministic rule.
- With evenly spaced samples and their rate, `sandbox_vibration_spectrum` gives the strongest frequencies. Compare
  them with the shaft, gear mesh and bearing defect frequencies the manual gives, cited.
- Without raw samples, say that a spectrum would separate the candidate modes, and which frequencies to look at.
- A rising bearing temperature can be projected with `sandbox_trend_fit`, against a cited alarm level. A projection is
  an estimate with its fit's R², never a prediction.

## Citing

- Take alert and alarm levels only from the approved manuals or the platform's own rules, cited.
