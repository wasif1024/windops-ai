# Gearbox Oil Cooling Fault: Diagnosis and Repair

| Field | Value |
| --- | --- |
| Document | WS-PRC-GBX-012 |
| Revision | 2 |
| Type | procedure |
| Published | 2024-11-05 |
| Issued by | WindOps Synthetic (fictional manufacturer) |
| Applies to | WS-8.0-167, WS-4.5-145 |
| Classification | internal |

> **Synthetic document.** Written for testing WindOps AI, about a fictional turbine from a fictional manufacturer. It
> describes no real product, and must not be used on real equipment.

## 1. When to use this procedure

Use it when `GBX_OIL_TEMP_HIGH` or `GBX_OIL_TEMP_CRITICAL` is raised, or when the gearbox oil runs hotter than its
history at the same load and air temperature.

## 2. Check remotely first

1. Compare the oil temperature with the load and the air temperature over the last day. A rise with the load, inside
   the normal range, is not a fault.
2. Compare the oil with the gearbox bearing temperature. If both have risen together, the oil is hot. If only the oil
   reading has moved, suspect the sensor and follow WS-PRC-SNS-040.
3. Check the cooler fan's run signal and current.

## 3. On the turbine

1. Check the oil level at the sight glass, with the turbine stopped for at least 10 minutes.
2. Check the cooler fan runs, and its current is within 10% of the motor plate.
3. Inspect the cooler's air side, and clean it with low-pressure air if fouled.
4. Check the thermostatic bypass valve moves freely.
5. Take an oil sample from the sampling valve for the laboratory.

## 4. Running on

With `GBX_OIL_TEMP_CRITICAL` raised, the turbine may run on at reduced power, derated to 60%, while the cause is
found, if the oil temperature stays below 72 °C.

## 5. Records

Record what was found and done against the turbine's case, with the oil sample's reference.
