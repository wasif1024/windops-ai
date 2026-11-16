# Gearbox Oil Cooling Fault: Diagnosis and Repair

| Field | Value |
| --- | --- |
| Document | WS-PRC-GBX-012 |
| Revision | 3 |
| Type | procedure |
| Published | 2026-04-08 |
| Issued by | WindOps Synthetic (fictional manufacturer) |
| Applies to | WS-8.0-167, WS-4.5-145 |
| Classification | internal |
| Supersedes | WS-PRC-GBX-012 revision 2 |

> **Synthetic document.** Written for testing WindOps AI, about a fictional turbine from a fictional manufacturer. It
> describes no real product, and must not be used on real equipment.

## What changed in revision 3

- Section 4: the turbine no longer runs on at reduced power with `GBX_OIL_TEMP_CRITICAL` raised. It stops, and a
  person decides on restart. Revision 2's derating is withdrawn.
- Section 3: check the cooler fan motor's run capacitor, after service bulletin WS-SB-2025-07.
- Section 3: the oil sample is taken before any oil is added or changed, so it shows the oil that ran hot.

## 1. When to use this procedure

Use it when `GBX_OIL_TEMP_HIGH` or `GBX_OIL_TEMP_CRITICAL` is raised, or when the gearbox oil runs hotter than its
history at the same load and air temperature.

## 2. Check remotely first

1. Compare the oil temperature with the load and the air temperature over the last day. A rise with the load, inside
   the normal range of 45 to 60 °C, is not a fault. A sustained strong wind at full load on a warm day sits at the
   top of the range and stays below the alarm.
2. Compare the oil with the gearbox bearing temperature. If both have risen together, the oil is hot. If only the oil
   reading has moved, and it keeps moving at a steady rate whatever the load, suspect the sensor and follow
   WS-PRC-SNS-040.
3. A failing cooler shows as oil that runs further above its usual level the higher the load: the cooler copes at
   low load and falls behind at high load.
4. Check the cooler fan's run signal and current.
5. Look at the CMS gearbox sensor (WS-ENG-CMS-001). Hot oil thins, and the gear mesh at 23 orders of the high-speed
   shaft grows a little. Bearing defect energy at 4.2 orders points to a bearing instead.

## 3. On the turbine

The turbine is stopped, isolated and locked for this work (WS-SAF-001).

1. Take an oil sample from the sampling valve for the laboratory, before adding or changing any oil.
2. Check the oil level at the sight glass, with the turbine stopped for at least 10 minutes.
3. Check the cooler fan runs, and its current is within 10% of the motor plate.
4. Check the fan motor's run capacitor against its rated capacitance. Replace it if it is more than 10% below, or
   swollen or leaking (WS-SB-2025-07).
5. Inspect the cooler's air side, and clean it with low-pressure air if fouled.
6. Check the thermostatic bypass valve moves freely and closes fully when hot.

## 4. A critical oil temperature

With `GBX_OIL_TEMP_CRITICAL` raised, the turbine stops. Running on at reduced power is not allowed: oil this hot
loses its film strength, and the gears and bearings wear quickly. A person with the authority to do so decides on
restart, once the cause is found and put right, and the oil has cooled below 60 °C.

## 5. After the repair

Run the turbine back to full load and watch the oil for at least 2 hours. It should settle inside its normal range,
and track the load and the air as it did before the fault.

## 6. Records

Record what was found and done against the turbine's case, with the oil sample's reference and the capacitor's
measured value.
