---
name: scada-analysis
description: How to read a wind turbine's SCADA telemetry, alarms and data quality for a fault investigation.
version: 1
---

# SCADA analysis

## Readings

- Read each metric with its unit, time and quality label. `good` readings are evidence; `suspect` and `missing`
  readings are data gaps, never evidence on their own.
- Compare a reading with the turbine's own rating and operating state. Power near zero in high wind while the state
  is `running` matters; the same reading while `stopped` does not.
- Look at related metrics together:
  - gearbox oil and bearing temperature against power and ambient temperature;
  - rotor and generator speed against each other: their ratio is fixed by the gearbox;
  - vibration RMS against rotor speed and power;
  - pitch angle against wind speed and power, above and below rated wind.

## Trends

- A trend needs at least three readings over the window. Say how many there were.
- Report a rise or fall with its start, size and rate, from the evidence snapshot's own items.

## Alarms

- Group repeats of one alarm code, with the count and the first and last times.
- Relate each alarm to the readings around it. An alarm with no supporting reading is worth naming as such.

## Data quality

- Report gaps, stale readings and quality labels before conclusions: a person must know what the analysis could not
  see.
