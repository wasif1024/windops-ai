# Temperature and Vibration Sensors: Verification, Drift and Dropout

| Field | Value |
| --- | --- |
| Document | WS-PRC-SNS-040 |
| Revision | 2 |
| Type | procedure |
| Published | 2025-10-02 |
| Issued by | WindOps Synthetic (fictional manufacturer) |
| Applies to | WS-8.0-167, WS-4.5-145 |
| Classification | internal |

> **Synthetic document.** Written for testing WindOps AI, about a fictional turbine from a fictional manufacturer. It
> describes no real product, and must not be used on real equipment.

## 1. When to use this procedure

Use it for the yearly verification, when a reading moves in a way the machine cannot, or when `SENSOR_SIGNAL_LOST`
is raised.

## 2. Drift: a reading that creeps away

A drifting sensor reads further from the truth over hours or days, at a steady rate, whatever the machine does. The
controller raises the same alarm for it as for a real fault, as it cannot tell them apart.

| Question | A real temperature rise | A drifting sensor |
| --- | --- | --- |
| Does it follow the load? | Yes: higher at high load, lower at low load | No: it moves at the same rate either way |
| Do the related readings move with it? | Yes: hot oil warms the gearbox bearing too | No: only the one reading moves |
| Does it move at a steady rate over hours? | Rarely | Yes, typically a few units an hour |
| Does the CMS show anything? | Often: more gear mesh, or bearing energy | Nothing |

A drifted reading that crosses an alarm level is not a machine fault, but it must still be put right: while it reads
wrong, it hides a real fault behind it.

## 3. Dropout: a sensor that sends nothing

A failing sensor, connector or cable sends nothing in stretches, short at first and more frequent as it worsens.

- Treat a lost reading as unknown, never as normal. The alarms that depend on it cannot be raised while it is lost.
- Repeated losses on one sensor, growing more frequent, need a visit. Losses on many sensors at once point to the
  turbine's network or its controller instead.

## 4. Verification on the turbine

The turbine is stopped, isolated and locked (WS-SAF-001).

1. Compare the sensor with a calibrated reference probe, placed beside it, once the part has settled. They should
   agree within 1 °C.
2. Check the connector for corrosion and the cable for damage along its run.
3. Replace a sensor that disagrees by more than 2 °C, or that drops out again after its connector is remade.
4. For an accelerometer, check its mounting is tight; a loose one reads high and noisy.

## 5. After the repair

Confirm the reading agrees with its history at the same load, and with the related readings, for at least a day.
Note on the case that the earlier readings were wrong, so they are not used as evidence of the machine's condition.
