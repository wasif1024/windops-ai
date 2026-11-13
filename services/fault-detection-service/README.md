# windops-fault-detection

Deterministic fault rules, and what they find in a turbine's recent telemetry and alarms (SRS 4.4, FR-FLT-001,
FR-FLT-002, FR-FLT-005). No model decides anything here: a rule is data, it is evaluated exactly as written, and a
finding's severity comes only from that rule's own levels. M5.3 records each detection, with the rule version and a
telemetry snapshot behind it.

## The kinds of rule (FR-FLT-001)

| Kind | Finds | Its figure |
| --- | --- | --- |
| `threshold` | A reading, or its rolling mean, at or above a level for long enough | The lowest value held |
| `residual` | A reading above what its normal behaviour model explains, for long enough | The smallest gap held |
| `rolling_zscore` | A recent mean far from its own baseline | Standard deviations |
| `alarm_correlation` | Alarms standing together, or an alarm with another rule's finding | 1, when it holds |
| `data_gap` | Too many of a reading's samples missing in a window | Percent missing |

A `residual` rule's model is either `thermal`, a part's first-order response to load squared and the air, or `speed`,
vibration's rise with rotor speed. Both take their figures from the turbine model's datasheet. A part that is hot
because it works hard is explained by its model, so it finds nothing; a failing cooler is not.

Each rule maps its figure to a severity through levels, such as 4 °C medium, 8 °C high and 15 °C critical. A higher
level can never map to a lower severity.

## The default library: `fault-rules-1`

A tenant starts with 12 rules, as version 1 of each, made by `system`. They cover the gearbox oil and bearings, the
main and generator bearings, vibration by its model and its baseline, a gearbox oil safety limit, the gearbox oil
alarm confirmed by its model, and missing sensor readings.

They were tuned against the simulator at 1 Hz, as it streams:

| Run | Found |
| --- | --- |
| A day of the healthy fleet, at two seeds | Nothing |
| Gearbox oil overheating | The oil and gearbox bearing residuals, up to critical, and the alarm correlation, from 2 h 10 m |
| Main bearing wear | The main bearing residual and vibration, from 4 h 20 m |
| Rotor imbalance | The vibration residual, from 2 h 20 m |
| Sensor drift | The gearbox oil residual: a drifting sensor and a hot gearbox look the same to a rule |
| Sensor dropout | The main bearing data gap |
| High load, healthy | Nothing: its heat is what its load explains |

## Versions, and who may change them (FR-FLT-002, FR-FLT-005)

Every change is a new version, with who made it, when and why, and a content hash over
`windops-fault-rule-v1|<rule>|<version>|<definition>`. An old version never changes: a database trigger refuses it,
even for this service's own login. Two people cannot overwrite each other: a change names the version it replaces,
and is refused if that is no longer current.

The SRS has no permission of its own for fault rules, so they are policy: reading needs `policy.read`, and a change
needs `policy.manage` and a recent second factor. That is the Tenant Administrator, by default, and never an agent or
a service, as neither has a person's second factor. Each change, and turning a rule on or off, is audited.

## A dry run

`EvaluateTurbine` runs every enabled rule against a turbine's recent telemetry and alarms, and records nothing. It
reads only the readings the rules use, for as long as the longest rule needs: about two hours. It asks the Asset,
Telemetry and Alarm services as the caller, so it sees only what they may see.

## Run the tests

```bash
uv run pytest services/fault-detection-service/tests
make fault-rules-check   # on the cluster: defaults, versions, who may change them, and a dry run
```
