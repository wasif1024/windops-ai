# Rotor Imbalance: Assessment and Blade Mass Balancing

| Field | Value |
| --- | --- |
| Document | WS-PRC-ROT-030 |
| Revision | 1 |
| Type | procedure |
| Published | 2025-05-12 |
| Issued by | WindOps Synthetic (fictional manufacturer) |
| Applies to | WS-8.0-167, WS-4.5-145 |
| Classification | internal |

> **Synthetic document.** Written for testing WindOps AI, about a fictional turbine from a fictional manufacturer. It
> describes no real product, and must not be used on real equipment.

## 1. When to use this procedure

Use it when `NACELLE_VIBRATION_HIGH` is raised with the drivetrain temperatures normal, or when the CMS main bearing
sensor shows a strong tone at once per revolution of the rotor.

## 2. Recognising imbalance

An unbalanced rotor pushes the nacelle round once per revolution, with a force that grows with the square of the
rotor speed.

| Sign | Imbalance | A bearing fault |
| --- | --- | --- |
| Nacelle vibration against rotor speed | Grows with the square of speed | Grows more slowly, and with temperature |
| CMS main bearing spectrum | A strong 1x tone | Impulsiveness, and the defect frequency |
| Bearing temperatures | Normal | The worn bearing runs hot |
| Kurtosis | About 3, as healthy | Above 4 |

Compare the vibration at the same rotor speed on earlier days. Imbalance from a blade does not come and go; ice
imbalance does, with the weather.

## 3. Causes

- A pitch angle error on one blade: aerodynamic imbalance.
- Water inside a blade, or a lost or added mass after a repair: mass imbalance.
- Ice on the blades, in freezing weather.
- Damage to a blade's leading edge or tip.

## 4. Assessment

1. Check the three blades' pitch angles against each other, with the rotor locked. A difference of more than 0.3°
   is an error to correct first.
2. Inspect the blades, from the ground or by drone, for damage and drain holes that are blocked.
3. If pitch and blades are sound, measure the imbalance with a once-per-revolution reference and the nacelle's
   vibration, at three or more rotor speeds.

## 5. Mass balancing

Balancing weights are fitted inside the blade root, under the manufacturer's instructions for the blade, by a team
qualified to enter the hub (WS-SAF-001). Measure again afterwards: the 1x vibration should fall by at least half.

## 6. Running on

A turbine with imbalance below the alarm may run while the work is planned. With `NACELLE_VIBRATION_HIGH` raised and
growing, stop it: imbalance loads the main bearing and the tower at every revolution.
