# windops-simulator

Synthetic turbine telemetry from versioned physical models, the same for a seed on every machine (SRS 10.4). Every
sample says it is synthetic (SIM-006). It stands in for real turbines, and the evaluations in Phase 12 use its known
ground truth.

## Try it

```bash
uv run python -m windops_simulator --seed 1001 --minutes 60 --every 600 --format table
uv run python -m windops_simulator --seed 1001 --minutes 10 --turbine dogger-east-t01 > sample.jsonl
```

The same seed, start and turbines always print the same samples. The default start is fixed for that reason: pass
`--start 2026-07-01T12:00:00+00:00` for another moment.

## What it models (SIM-003)

Models version `1.0.0`, in `models.py`. Any change to them changes the version, which every sample carries.

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
| `seed`, `model_version`, `schema_version`, `source`, `synthetic` | What reproduces it, and `synthetic: true` |

## The demo fleet

`DEMO_TURBINES` matches the Asset Service's demo seed (`make asset-seed`), turbine for turbine: 8 offshore turbines of
8 MW for north-sea, and 5 onshore of 4.5 MW for atlas-wind. A test keeps the two the same.

## Run the tests

```bash
uv run pytest simulator/tests
```

## Still to come

- **Fault profiles** and injected data problems, with hidden ground truth (M4.3).
- **Sending** samples to the Telemetry Service, from its own workload, as an authenticated device (M4.4).
- **10-minute SCADA summaries and CMS vibration spectra** come from the Telemetry Service (M4.5).
