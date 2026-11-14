# windops-policy

Versioned thresholds, and deterministic risk, safety, weather-window, business and escalation checks on a proposed
action (SRS 4.7, FR-POL-001 to FR-POL-005). No model decides anything here. A proposal gets pass, block or escalate,
with every rule version and input it was judged on, and the result never changes.
[ADR 0009](../../docs/adr/0009-policy-judges-a-proposal-on-facts-it-gathers.md) records the design.

## What is judged, and on what

A caller sends only the **proposal**: its action, whether it needs work at height, a planned window, and an
estimated cost, downtime and confidence. The service gathers every other input **as itself**, with fixed read
grants:

| Input | From |
| --- | --- |
| `case.status`, `case.severity`, `evidence.status`, `evidence.age_hours` | Investigation |
| `turbine.status`, and the turbine's site | Asset |
| `weather.max_wind_speed_mps`, `weather.max_gust_mps` | Weather, from M6.3. Until then, always `unavailable` |

The caller must be able to read the case's turbine (`investigation.read`), or the case is not found.

## The kinds of rule

| Kind | Crossed when | Options |
| --- | --- | --- |
| `limit` | A number is `above` or `below` the limit | `sites`: a limit of its own for a wind farm (FR-POL-001) |
| `match` | A value is `in`, or `not_in`, a list | |

Any rule may apply only `when` another input equals a value, and may refuse an input older than `max_age_seconds`.
Crossing it gives its `outcome`, block or escalate. The result is the worst outcome: block, then escalate, then pass.

## Failing closed (FR-POL-005)

- **When an input can't be used, the rule's `when_missing` outcome applies.** That covers an input that is missing,
  stale, of the wrong type, or from an owner that can't be reached. A definition whose `when_missing` isn't block or
  escalate is refused, so no rule can pass on an input it doesn't have.
- **A condition that can't be read is assumed to hold**, so the rule still applies.
- **A site that can't be read gets the strictest of a rule's limits.**
- **If the Investigation Service can't be reached, nothing is judged:** the call answers UNAVAILABLE, and no result
  means no recommendation reaches `pending_review` (SRS 7.7).

## The default library: `policy-rules-1`

A tenant starts with 10 rules, as version 1 of each, made by `system`. They are starting values for review, not a
site's approved figures:

| Rule | Category | Outcome |
| --- | --- | --- |
| `work-at-height-wind` | weather_window | Block above 12 m/s, for work at height; a forecast older than 3 h is stale |
| `work-at-height-gusts` | weather_window | Block above 18 m/s gusts, for work at height |
| `evidence-ready` | evidence | Block unless the case's evidence is frozen |
| `evidence-fresh` | evidence | Block if the evidence is more than 24 h old |
| `case-active` | escalation | Block unless the case is open, investigating or pending review |
| `critical-case` | risk | Escalate a critical case |
| `turbine-offline` | safety | Escalate if the turbine is offline |
| `low-confidence` | risk | Escalate below 0.6 confidence |
| `cost-limit` | business | Escalate above €50,000 |
| `downtime-limit` | business | Escalate above 48 h of downtime |

## Versions, results, and who may do what

- **Reading rules** needs `policy.read`.
- **Changing one** needs `policy.manage` and a recent second factor (SEC-003), so only a person can, never an agent
  or a service. Each change is a new, hashed version, and each change is audited.
- **Each evaluation is a result:** `pol_` and 20 characters, hashed over its canonical JSON and audited as
  `policy.evaluated`. Reading one checks its hash again. A retry with the same `idempotency_key` returns the first
  result.
- **Nothing changes once written:** a database trigger refuses to change or delete a rule version or a result, even
  for the service's own login.

## Run the tests

```bash
uv run pytest services/policy-service/tests
make policy-check   # on the cluster: a real case judged, a site's own limit, and PostgreSQL refusing changes
```

## Build the image

```bash
docker build -f services/policy-service/Dockerfile -t windops-policy:local .
```
