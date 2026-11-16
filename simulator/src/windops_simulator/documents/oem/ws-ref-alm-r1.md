# Controller Alarm Reference

| Field | Value |
| --- | --- |
| Document | WS-REF-ALM |
| Revision | 1 |
| Type | reference |
| Published | 2025-02-20 |
| Issued by | WindOps Synthetic (fictional manufacturer) |
| Applies to | WS-8.0-167, WS-4.5-145 |
| Classification | internal |

> **Synthetic document.** Written for testing WindOps AI, about a fictional turbine from a fictional manufacturer. It
> describes no real product, and must not be used on real equipment.

## 1. How the controller raises alarms

The controller watches what its sensors report, and applies the alarm rules `controller-alarms-1`. An alarm is
raised once its condition has held for 10 seconds, and cleared once the reading has stayed below its clear level for
30 seconds, so a reading near a threshold does not make the alarm flicker. A cleared alarm stays in the history.

The controller judges the reading it receives. It cannot tell a hot gearbox from a sensor that reads high: an alarm
says a reading crossed a level, not why.

## 2. Alarms

| Code | Raised at | Cleared below | Severity |
| --- | --- | --- | --- |
| `GBX_OIL_TEMP_HIGH` | 63 °C | 60 °C | high |
| `GBX_OIL_TEMP_CRITICAL` | 69 °C | 66 °C | critical |
| `GBX_BEARING_TEMP_HIGH` | 70 °C | 68 °C | high |
| `MAIN_BEARING_TEMP_HIGH` | 42 °C | 39 °C | high |
| `GEN_BEARING_TEMP_HIGH` | 65 °C | 62 °C | high |
| `NACELLE_VIBRATION_HIGH` | 3.0 mm/s | 2.5 mm/s | medium |
| `SENSOR_SIGNAL_LOST` | 10 seconds with no signal | a signal again | low |

## 3. What each alarm means, and what to do first

### `GBX_OIL_TEMP_HIGH`

The gearbox oil is hotter than the cooling system should allow. Likely causes: a failed or weak cooler fan, a
fouled cooler, a stuck thermostatic bypass valve, low oil level, or a sensor reading high. First check the cooler
fan's state and current, the oil level, and whether the gearbox bearing temperature has risen with the oil. Follow
WS-PRC-GBX-012. If only the oil reading has moved, and the bearing and the load have not, suspect the sensor
(WS-PRC-SNS-040).

### `GBX_OIL_TEMP_CRITICAL`

The oil is hot enough to lose its film strength. The turbine must be stopped, and not restarted until the cause is
found and put right. Follow WS-PRC-GBX-012, section 4.

### `GBX_BEARING_TEMP_HIGH`

A gearbox bearing is running hot. With the oil hot too, the cause is usually the oil. With the oil normal, suspect
the bearing itself: check the CMS gearbox sensor for bearing defect energy (WS-ENG-CMS-001), and take an oil sample
for wear metals.

### `MAIN_BEARING_TEMP_HIGH`

The main bearing is running hot. Causes: wear, too little or too much grease, or contaminated grease. Follow
WS-PRC-BRG-021.

### `GEN_BEARING_TEMP_HIGH`

The generator's drive-end bearing is running hot. Check its grease, and the CMS generator sensor.

### `NACELLE_VIBRATION_HIGH`

The nacelle is vibrating more than a healthy drivetrain should. If it rises with the square of rotor speed and the
temperatures are normal, suspect rotor imbalance (WS-PRC-ROT-030). If it comes with a bearing temperature rise,
suspect the bearing. On the WS-4.5-145, consider ice on the blades first in freezing weather.

### `SENSOR_SIGNAL_LOST`

A sensor sent nothing for 10 seconds. Repeated losses that grow more frequent point to a failing sensor, connector
or cable. Follow WS-PRC-SNS-040. While a signal is lost, the alarms that depend on it cannot be raised: treat the
reading as unknown, never as normal.

## 4. Severity

| Severity | Meaning |
| --- | --- |
| critical | Stop the turbine; a person decides on restart |
| high | Investigate within the shift; plan work |
| medium | Investigate within days |
| low | Note, and act if it repeats |
