# windops-production-impact

Deterministic energy, availability and financial impact for a proposed action (SRS 4.6, FR-IMP-001 to FR-IMP-003). No
model computes or changes a figure here: the Production Impact agent may only explain a result (FR-IMP-004). Every
result keeps its inputs, the formula version, its assumptions and a low to high range for each figure, and never
changes.

## What an estimate rests on

A caller sends only the case, the action and how long the turbine will be down. The service gathers the rest **as
itself**, with fixed read grants (`turbine.read`, `telemetry.read`, `investigation.read`):

| Input | From |
| --- | --- |
| The case's turbine | Investigation |
| The turbine's model, its site, and whether the site is offshore | Asset |
| The last 6 hours of wind and power, at 1 Hz | Telemetry |
| The model's power curve, the energy price and the maintenance costs, each at its current version | This service's reference data |

The caller must be able to read the case's turbine (`investigation.read`), or the case is not found. Without the
telemetry, a power curve for the model, or at least 10 minutes of wind and power, there is no estimate, never a
guess (SRS 7.7).

## The formulas: `impact-formulas-1`

| Figure | How |
| --- | --- |
| `observed_lost_energy_mwh` | What the power curve says the observed wind should have produced, less what was produced, never below 0 |
| `observed_availability_pct` | Time producing, as a share of time with wind between cut-in and cut-out |
| `projected_lost_energy_mwh` | The window's mean expected output, over the planned downtime |
| `availability_impact_pct_points` | The downtime, as a share of a 730-hour month |
| `lost_revenue_eur` | The projected lost energy at the site's energy price |
| `maintenance_cost_eur` | Labour hours at the labour rate, and access days at the onshore or offshore day rate |
| `total_cost_eur` | Lost revenue and maintenance together |

- **The range:** each figure's low and high take the anemometer's ±0.5 m/s and the power curve's tolerance together,
  at their worst in each direction.
- **Gaps:** a sample stands for the time to the next one, up to 60 seconds, so a gap is missing data, not output.
- **Assumptions:** each estimate states its own in words, among them that the wind holds as observed until the
  Weather Service's forecasts arrive (M6.3), and that parts are priced once the Inventory Service arrives (Phase 9).

## The reference data: `impact-references-1`

A tenant starts with four references, as version 1 of each, made by `system`:

| Reference | Holds |
| --- | --- |
| `power-curve-sg-8-0-167`, `power-curve-sg-4-5-145` | The two synthetic demo models' curves, every 0.5 m/s from cut-in to cut-out, tabulated from the simulator's own physics, within ±5% |
| `energy-price` | 75 EUR/MWh, with a price of its own for any site |
| `maintenance-costs` | 95 EUR an hour of labour; 1,800 EUR a day onshore and 14,000 EUR a day offshore; the hours and days each action takes |

The price and costs are placeholders for review, not market figures. Reading them needs `policy.read`. Publishing a new
version needs `policy.manage` and a recent second factor (SEC-003), and is audited. A database trigger refuses to
change or delete a reference version or a result, even for the service's own login.

## How the Policy Service uses it

The Policy Service's cost and downtime rules judge a Production Impact estimate, not the caller's own figures: the
high end of its total cost, and its downtime ([ADR 0009](../../docs/adr/0009-policy-judges-a-proposal-on-facts-it-gathers.md)).
Without a usable estimate, both escalate.

## Run the tests

```bash
uv run pytest services/production-impact-service/tests
make impact-check   # on the cluster: an estimate from live telemetry, a site's own price, and Policy judging it
```

## Build the image

```bash
docker build -f services/production-impact-service/Dockerfile -t windops-production-impact:local .
```
