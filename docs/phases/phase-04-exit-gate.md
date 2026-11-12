# Phase 4 exit gate

**Result:** passed on 2026-11-12, on the local Minikube cluster, in 14 minutes, with all 11 checks.

SRS section 12 sets the gate: *a seeded fault profile streams into storage with the correct quality labels.* Run it
yourself with:

```bash
make dev
make phase-4-gate
```

## The gate itself: a seeded fault, matched to its ground truth

`make fault-stream-check` gives a check-only turbine, `gate-check-t01` in the tenant `telemetry-check`, a device
credential. It then runs a seeded scenario in real time: the gearbox oil overheating profile from `gearbox-temp-001`,
sped up to start a minute in and reach full heat 3 minutes later. The link is poor on purpose: about 1% of samples
are lost, sent twice, held back 30 to 90 seconds, broken or sent after the next one.

The device sends each batch through the gateway when the simulator says it is sent, so the delayed samples really
arrive late. Once their 10-minute periods close, everything stored is compared with the simulator's hidden ground
truth, which never travels with the data.

Output from the passing run:

```text
info - streaming 598 deliveries for gate-check-t01, from 20:36:48, until 20:47:53
info - waiting until 20:51:05 for the last period to close
ok - The ground truth has every kind of problem: {'delayed': 4, 'duplicated': 5, 'malformed': 3, 'missing': 7, 'out_of_order': 3}
ok - Every stored sample has a label
ok - Every well-formed delivery was stored once: 595 samples
ok - The 5 samples sent twice: each copy is a duplicate
ok - The 4 samples held back 30 to 90 seconds are delayed
ok - The 7 samples never sent, and the 1 cut off part way, are gaps, and nothing else is
ok - 3 of 3 samples sent after the next are out_of_order; the rest followed a sample that never arrived in time
ok - The 3 broken samples are refused, and kept by sequence where it could be read
ok - The 10-minute summaries count the same gaps, duplicates and delays
ok - Every stored reading is the one the simulator measured
ok - The fault reached storage: gearbox oil 46.9 °C before it, 51.1 °C at full heat
Fault stream check passed
```

| Part of the gate | What the check requires |
| --- | --- |
| A seeded fault profile | A fixed seed, so the problems fall on the same sequences on every run, and the gearbox fault's oil temperature rises in storage by at least 2 °C |
| Streams into storage | Every well-formed delivery is stored once, through the real gateway and device route, and every stored reading is the one the simulator measured |
| With the correct quality labels | Each injected problem has its label, gap or rejection, and nothing else does. The 10-minute summaries count the same |

`tests/test_fault_stream.py` replays the same scenario through the real device route and summarizer in seconds, so a
change to the simulator, its seed or the labelling rules that would fail the gate fails in CI first.

## Every Phase 4 check, in the same run

The fault stream spends most of its time waiting for periods to close, so `make phase-4-gate` runs it alongside the
other checks. It uses its own turbine, and reaches the gateway on its own local port.

```text
ok - asset-check (24 s): wind farms, turbines and components, isolated by tenant
ok - telemetry-check (17 s): device credentials, the device route and storage
ok - telemetry-storage-check (39 s): partitions, 10-minute summaries, late data and retention
ok - cms-check (1 s): CMS vibration features, a worn bearing's signature and raw windows
ok - data-quality-check (200 s): one of each data problem, sent and labelled
ok - snapshot-check (16 s): telemetry read back, and frozen into reproducible snapshots
ok - freshness-check (216 s): the Data Freshness dashboard and its alerts
ok - simulator-check (75 s): the simulator streams the demo fleet, and survives a restart
ok - route-sweep (12 s): every route, the fleet screens' too, refuses who it must
ok - frontend-check (130 s): the fleet screens in a real browser, phone and desktop
ok - fault-stream-check (881 s): a seeded fault profile streams into storage with the correct quality labels
Phase 4 exit gate passed in 14 minutes: all 11 checks
```

## The earlier phases, on the same cluster

Phase 4 changed things earlier phases depend on:
- every service's PostgreSQL connection (#87);
- Web BFF, with the fleet routes;
- the Prometheus and Grafana configuration.

So the earlier gates ran again, with the Phase 3 checks that cover those changes. They ran between the Phase 4
gate's runs, before its last fixes, which changed only the Telemetry Service's memory limits and Phase 4's own checks:

| Command | Result |
| --- | --- |
| `make phase-1-gate` | Passed: a traced call through the gateway, and its breaker opening and recovering |
| `make phase-2-gate` | Passed: an event survives a NATS restart, and redelivery creates one record |
| `make cross-tenant-check` | Passed: 10 tables and 20 API attempts across tenants, all refused |
| `make authorization-check`, `make audit-check` | Passed |
| `make edge-check`, `make bff-check`, `make gateway-policy-check` | Passed |
| `make security-check` | Passed |
| `make check`: lint, types, contracts and 1,539 tests; frontend; dependency audits | Passed |

The Phase 2 gate also printed `NATS connection error: SSLError` twice. That is the gate's own client logging its TLS
connection closing when its port-forward ends, after the steps that use it. Each step had already passed.

The Phase 3 checks not run again cover what Phase 4 did not touch: Keycloak and MFA, mutual TLS, data store
certificate renewals, token exchange, and the administrator and Platform consoles.

## What the gate found on the way

The gate failed four times before it passed, each time for a reason now fixed:

| Found | Fixed |
| --- | --- |
| The Telemetry Service was killed for memory: it uses about 170Mi, its limit was 192Mi, and upkeep run in the pod by a check adds a second process | Its limit is 384Mi, with a 192Mi request, measured on the demo fleet |
| One 502 while the service's readiness probe was slow ended the fault stream | The check's device retries briefly, as a real one would, staying under the 20 seconds that would make a sample delayed |
| Restarting the simulator zeroes its CMS counts, sent every 10 minutes, so the CMS check could not see them | The simulator check runs after the CMS check |
| The freshness check counted other checks' turbines as ones that must be live | Only the demo fleet must be live |
| Two checks shared local port 9443, and one lost it when the other ended | The fault stream reaches the gateway on a port of its own |
| The storage check's summarizer run could be skipped while the service's own held the lock, and it timed its samples before they were stored | It tries again, and the database stamps when they arrive |

Twice the cluster also needed the maintenance it needs after about 3 days up: cert-manager had restarted 112 times.
Restarting the system workloads in `kube-system` and `cert-manager` cleared it.

## Next

The summarizer reads samples by `ingested_at`, which a device route stamps just before its transaction commits. A
summarizer run in that moment could pass a sample by. None was found in 3 hours of live data, 139,669 samples, but a
watermark that trails a little would close it. That needs a summary to be written again only when it changes,
so it is left for a later module.
