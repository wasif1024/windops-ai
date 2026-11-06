# windops-simulator

Synthetic turbine telemetry from versioned physical models, the same for a seed on every machine (SRS 10.4). Every
sample says it is synthetic (SIM-006). It stands in for real turbines, and the evaluations in Phase 12 use its known
ground truth.

## Try it

```bash
uv run python -m windops_simulator --seed 1001 --minutes 60 --every 600 --format table
uv run python -m windops_simulator --seed 1001 --minutes 10 --turbine dogger-east-t01 > sample.jsonl
uv run python -m windops_simulator --list-scenarios
uv run python -m windops_simulator --scenario gearbox-temp-001 --ground-truth truth.json > sent.jsonl
```

The same seed, start and turbines always print the same samples. The default start is fixed for that reason: pass
`--start 2026-07-01T12:00:00+00:00` for another moment.

## What it models (SIM-003)

Models version `1.1.0`, in `models.py` and `faults.py`. Any change to them changes the version, which every sample carries.

| Model | How |
| --- | --- |
| Wind | Each wind farm's mean wind wanders slowly around its site's mean (9.5 m/s offshore, 7 m/s onshore), and its turbines share it. Each turbine adds its own turbulence, stronger onshore. The direction wanders around the south-westerly |
| Power | The cubic law, `½ ρ A Cp v³` with Cp 0.45 and 94% drivetrain efficiency, from cut-in at 3 m/s up to the turbine's rated power, flat to cut-out at 25 m/s, then nothing. The rotor's inertia smooths the wind it turns into power |
| Rotor and generator speed | The best tip-speed ratio, 8, below a tip speed of 90 m/s, then held there. The generator turns 100 times faster, through the gearbox |
| Pitch | Flat below rated wind, then turned out of the wind to hold rated power, and feathered when stopped |
| Temperatures | Gearbox oil and bearings, and the main and generator bearings, each a first-order model: they move toward a target set by load and ambient air, over 20 to 40 minutes |
| Ambient air | Colder at higher latitudes, warmest in late July and in mid-afternoon, local solar time |
| Vibration | Nacelle RMS velocity rising with rotor speed, inside the ISO 10816-21 healthy range |
| State | `producing`, `idle_low_wind` or `stopped_high_wind`, or the turbine's Asset status when it is not operating, with availability |

## Each sample

| Field | Unit or values |
| --- | --- |
| `tenant_id`, `turbine_id`, `event_time` | The turbine, and the time, in UTC |
| `wind_speed_mps`, `wind_direction_deg` | m/s at hub height, and degrees from north |
| `rotor_speed_rpm`, `generator_speed_rpm`, `pitch_angle_deg` | rpm, rpm, degrees |
| `power_kw` | Electrical output |
| `ambient_temp_c`, `gearbox_oil_temp_c`, `gearbox_bearing_temp_c`, `main_bearing_temp_c`, `generator_bearing_temp_c` | °C, to a tenth |
| `nacelle_vibration_mm_s` | mm/s RMS |
| `operating_state`, `available` | See above |
| `sequence` | Counts up by one for each sample a turbine makes, as a device numbers its messages |
| `seed`, `model_version`, `schema_version`, `source`, `synthetic` | What reproduces it, and `synthetic: true`. Schema version `2` |

A reading is `null` while its sensor sends nothing.

## Faults (SIM-002)

Each starts at its onset, and grows to its full severity over its ramp. Faults draw from their own random streams, so a
turbine's data is the same with or without a fault until it begins.

| Profile | What changes | Expected label |
| --- | --- | --- |
| `gearbox_oil_overheating` | Failing cooling: the oil runs up to 28 °C hotter, more under load, and its bearing and vibration a little | `gearbox_oil_overheating` |
| `bearing_wear` | The main, gearbox or generator bearing heats by up to 20 °C and adds broadband vibration, accelerating as wear does | `bearing_wear` |
| `imbalance` | Vibration growing with the square of rotor speed, with temperatures normal | `rotor_imbalance` |
| `sensor_drift` | One sensor's reading creeps away, 2.5 units an hour, while the machine is healthy | `sensor_drift` |
| `sensor_dropout` | One sensor sends nothing, in stretches of about two minutes, more often as it worsens | `sensor_dropout` |
| `false_positive_high_load` | A sustained strong wind: a healthy turbine at full load, running hot but in its normal range | `no_fault` |

## Data problems (SIM-004)

Applied to what a device sends, not to the physics, at the rate asked for each:

| Problem | What is sent |
| --- | --- |
| `missing` | Nothing |
| `duplicated` | The sample twice |
| `delayed` | The sample, 30 seconds to 10 minutes late |
| `malformed` | The sample broken: `event_time` missing, `power_kw` as text, or JSON cut off part way |
| `out_of_order` | The sample just after the one that follows it |

## Scenarios and ground truth (SIM-005)

A scenario is a seed, turbines, faults and data problems. Running one gives what its devices send, in the order sent,
and a ground truth document: the expected label, each fault's turbine, component, onset and full severity, when a good
detector could first see it, and every injected data problem by turbine and sequence. The ground truth is for the
evaluation system, never the agents, so the command writes it only to the file named by `--ground-truth`, and nothing
in the data gives it away. A test checks that.

| Scenario | Seed | What |
| --- | --- | --- |
| `gearbox-temp-001` | 1001 | Failing gearbox oil cooling on dogger-east-t01, the SRS's example case |
| `bearing-wear-001` | 1002 | Main bearing wear on hornsea-west-t02, over 8 hours |
| `imbalance-001` | 1003 | Rotor imbalance on atlas-ridge-t03 |
| `sensor-drift-001` | 1004 | The gearbox oil sensor drifting on dogger-east-t02 |
| `sensor-dropout-001` | 1005 | The main bearing sensor dropping out on hornsea-west-t03 |
| `high-load-001` | 1006 | The false positive on dogger-east-t03 |
| `data-quality-001` | 1007 | All of north-sea, healthy, over a poor link with every data problem |

Every fault scenario also has light link trouble, so detection is never tested on clean data.

## The demo fleet

`DEMO_TURBINES` matches the Asset Service's demo seed (`make asset-seed`), turbine for turbine: 8 offshore turbines of
8 MW for north-sea, and 5 onshore of 4.5 MW for atlas-wind. A test keeps the two the same.

## Run the tests

```bash
uv run pytest simulator/tests
```

## Still to come

- **Sending** samples to the Telemetry Service, from its own workload, as an authenticated device (M4.4).
- **10-minute SCADA summaries and CMS vibration spectra** come from the Telemetry Service (M4.5).
