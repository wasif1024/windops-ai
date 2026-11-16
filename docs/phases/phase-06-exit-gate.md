# Phase 6 exit gate

**Result:** passed on 2026-11-16, on the local Minikube cluster, with all 10 checks. The full gate run found one fault,
in the audit check itself, now fixed, so that check was run again on its own (see below).

SRS section 12 sets the gate: *boundary tests pass 100%, and a missing weather input fails closed.* Run it yourself
with:

```bash
make dev
make phase-6-gate
```

## Boundary tests pass 100%

`make boundary-check` runs every Phase 6 boundary test and passes only at 100%. Any skipped or erroring test counts
against it, as a failing one does.

```text
ok - 97 boundary tests in 3 suites, 100% passed
Boundary check passed
```

`tests/test_phase_6_boundaries.py` generates its cases from the default libraries, so a rule or reference added or
changed later is covered without editing the suite:

| For | Every case |
| --- | --- |
| Each limit rule | Exactly at the limit passes; a millionth inside passes; a millionth outside gives the rule's outcome |
| Every rule | A missing, unavailable or stale input gives its `when_missing` outcome, never pass |
| Each rule with an age limit | At its oldest the input is used; a second later it is stale |
| Each match rule | A listed value gives its outcome; an unlisted one passes |
| Each rule for work at height | Not applicable on the ground; still applied when that cannot be read |
| Each weather rule | Weather from anywhere but the Weather Service never passes (FR-POL-004) |
| Each power curve | Zero just below cut-in and from cut-out; rated just below cut-out |
| Weather freshness | The Weather Service's stale limit matches Policy's age limit, and it refreshes well before it |

With them run the rule engine's own tests (`services/policy-service/tests/test_rules.py`) and the impact formulas'
(`services/production-impact-service/tests/test_formulas.py`), including ranges that hold their value near cut-out.

## A missing weather input fails closed

`make weather-outage-check` uses a check-only turbine at a site of its own, in the tenant `telemetry-check`, so no
forecast is kept for it. It judges a proposal to climb it as the weather goes missing in each way it can.

- **The provider is made unreachable** by pointing the Weather Service at a closed address.
- **The service is taken down** by scaling it to zero.

Both are put back however the check ends. Output from the passing run:

```text
ok - With the provider unreachable and nothing kept, the forecast is unavailable: The provider cannot be reached right now
ok - and the climb blocks: weather.max_wind_speed_mps is unavailable: The provider cannot be reached right now
ok - With only a 4-hour-old calm forecast kept, it is stale, never current: The provider cannot be reached right now; the newest forecast is 4.0 h old, more than 3 h
ok - and the climb blocks, though that forecast's wind is calm: weather.max_wind_speed_mps is stale: The provider cannot be reached right now; the newest forecast is 4.0 h old, more than 3 h
ok - With the provider back, the forecast is fresh, 0 s old
ok - Policy has the forecast's hub wind: weather.max_wind_speed_mps is 12.633, above 12
ok - and the climb is judged on it: 12.633 m/s at the hub, so block
ok - With the Weather Service down, a climb in telemetry-check blocks: weather.max_wind_speed_mps is unavailable: The Weather Service cannot be reached right now
ok - With the Weather Service down, a climb in north-sea blocks: weather.max_wind_speed_mps is unavailable: The Weather Service cannot be reached right now
ok - and Production Impact holds the observed wind, saying why: The wind over the 12 h of downtime stays as observed over the 2.5 h window, as the Weather Service gave no forecast: The Weather Service cannot be reached right now
ok - With the Weather Service back, climbs are judged on its forecast again: block
Weather outage check passed
```

| Weather missing as | What the gate requires |
| --- | --- |
| The provider unreachable, nothing kept | The forecast is `unavailable`, and the climb blocks |
| Only a stale forecast kept | It is `stale`, never presented as current (WTH-005), and the climb blocks, though that forecast was calm |
| The Weather Service down | The climb blocks in both tenants, and Production Impact holds the observed wind, saying why, rather than guessing (SRS 7.7) |
| Each one put right | The forecast is fresh again, and the climb is judged on it |

On the day, the real forecast at the gate's site was 12.6 m/s at the hub, over the 12 m/s limit, so even with the
weather back the climb was blocked, this time on its merits.

## Every Phase 6 check, and the earlier ones Phase 6 touched

Phase 6 changed several things earlier phases depend on:
- Authorization's service grants, for Policy, Production Impact and Weather;
- the audit trail, which the new services write to;
- the migrations library, which now declares `sqlalchemy[asyncio]`.

So the gate runs the checks covering those with its own:

| Check | Proves | Result |
| --- | --- | --- |
| `edge-check` | The gateway serves only HTTPS, and only Keycloak's sign-in pages | Passed in the gate run |
| `boundary-check` | Every boundary test, generated from the default libraries, passes 100% | Passed in the gate run |
| `policy-check` | Proposals judged on gathered facts and the real forecast, recorded for ever | Passed in the gate run |
| `impact-check` | Estimates with ranges from live telemetry and the forecast, judged by Policy | Passed in the gate run |
| `weather-check` | A real Open-Meteo forecast, with its provenance and freshness | Passed in the gate run |
| `authorization-check` | Roles, scopes, break-glass, service grants and audited denials | Passed in the gate run |
| `audit-check` | A tamper-evident audit record, announced through the outbox | Passed, run on its own |
| `cross-tenant-check` | Every data path attacked across tenants, and refused | Passed in the gate run |
| `migrate-check` | Migration Jobs run once per release | Passed in the gate run |
| `weather-outage-check` | A missing weather input fails closed, each way it can go missing | Passed in the gate run |
| `make check`: lint, types, contracts and 1,776 tests; frontend; dependency audits | | Passed |

The full gate run took 8 minutes. The checks not run again cover what Phase 6 didn't touch: the gateway's policies,
Keycloak and MFA, the frontend, the telemetry pipeline, and the Phase 5 fault-to-case path.

## What the gate found on the way

| Found | Fixed |
| --- | --- |
| The audit check read `audit.recorded` from the start of the event stream, at most 2,000 messages, for one in its run's own tenant. The stream now holds days of every service's announcements, and Phase 6 added many, so today's were out of reach. The Audit Service had published them all: its outbox held none unsent | The check reads from when it began |
| The outage check, as first written, judged a climb the moment the Weather Service's pod was replaced, when Policy still reached the old one, and failed closed as it should | It waits, up to 90 seconds, for Policy to reach the new pod, as Policy's breaker lets a probe through after 30 seconds |

## Next

Phase 7, knowledge and external intelligence, starts with M7.1.
