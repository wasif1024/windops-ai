# Phase 5 exit gate

**Result:** passed on 2026-11-14, on the local Minikube cluster, with all 18 checks. The full gate run found three
problems, all now fixed, so four checks were run again on their own afterwards (see below).

SRS section 12 sets the gate: *a fault creates one deduplicated case bound to an immutable snapshot.* Run it yourself
with:

```bash
make dev
make phase-5-gate
```

## The gate itself: a seeded fault, found by the default rules, makes one case

`make fault-case-check` makes a check-only turbine for each run in the tenant `telemetry-check`, with the demo
turbine's model, and gives it a device credential. The turbine runs the seeded gearbox oil overheating scenario,
`gearbox-temp-001`, through the gateway's device route. Nothing is tuned for the gate: the tenant's default rule
library `fault-rules-1`, the detector and the case rules `case-rules-1` are the ones every tenant has.

The normal behaviour models need two thermal time constants of history before they judge: an hour for the gearbox
oil. So the device first sends the hour before the fault, as a turbine that had been streaming would. The detector is
held back until that hour is in, so it reads the hour whole. Then the fault streams live, sped up to reach full heat 3
minutes after it starts.

Output from the passing run:

```text
info - case-gate-938ef4 streams the gearbox fault from 06:28:39, after an hour of history
info - sent the hour before the fault: 3600 samples
info - 21 min into the fault: 1 case(s), found {'gearbox-oil-residual': 'medium'}
info - 27 min into the fault: 1 case(s), found {'gearbox-oil-residual': 'high', 'gearbox-bearing-residual': 'medium'}
ok - The fault made one case, and only one: 1
ok - The detector opened it, for the gearbox: Gearbox: gearbox-oil-residual
ok - Nothing was found in the hour before the fault; the first detection came 21 minutes after it began
ok - The default rules found it on their own: gearbox-bearing-residual v1 medium, gearbox-oil-residual v1 high, gearbox-oil-residual v1 medium
ok - All 3 detections, from both rules and each change of severity, joined that one case
ok - The case is at the highest severity found: high
ok - The detector looked again and still found it, and kept 2 detection(s) rather than adding any: one open detection per rule
ok - PostgreSQL holds only that case for the turbine
ok - Its newest evidence, version 2, holds the turbine, its telemetry, all 3 detections and their exact rule versions: 7 items, every hash verifying
ok - The Telemetry Service verifies the 5143 samples it froze for it
ok - Each of the 1 earlier version(s) still verifies, unchanged since
ok - PostgreSQL refuses to update it, even for the service's own login
ok - PostgreSQL refuses to delete it, even for the service's own login
Fault case check passed
```

| Part of the gate | What the check requires |
| --- | --- |
| A fault | The seeded gearbox scenario, sent through the real gateway and device route. Nothing is found in the hour before it, and the unchanged default rules find it: the gearbox oil and gearbox bearing models, at rising severity |
| Creates one case | The detector opens exactly one case, for the gearbox, at the highest severity found. PostgreSQL holds only that case for the turbine, and its `cases_one_active` index allows no second active one |
| Deduplicated | Every detection joins that case: both rules' detections, and the new detection when the oil model's severity rose. A later look still finds the fault and keeps the open detections, adding none: one open detection per rule |
| Bound to an immutable snapshot | The newest evidence holds every detection, the exact rule versions, the turbine and its frozen telemetry, every hash verifying. The earlier version still verifies. PostgreSQL refuses to update or delete it, even for the service's own login |

The run takes about 30 minutes, nearly all of it the fault streaming live. `tests/test_fault_case.py` replays the same
seeded scenario in under a second through the default rules, at the detector's 5-minute looks, and checks the case
rules put everything in one gearbox case. A change to the simulator, its seed, the default rules or the case rules
that would fail the gate fails in CI first.

The timings were measured offline before the run, and the cluster matched them:

| | Offline, at 1 Hz | On the cluster |
| --- | --- | --- |
| Gearbox oil model, medium | 17 minutes into the fault | Found at the detector's look at 21 minutes |
| Gearbox bearing model, medium, and oil at high | 22 and 23 minutes | Found at the look at 27 minutes |

## Every Phase 5 check, and the earlier ones Phase 5 touched

`make phase-5-gate` runs the fault case alongside the other checks.

Phase 5 changed several things earlier phases depend on:
- the shared gRPC and identity libraries;
- Authorization, which gained fixed read grants for services;
- the Telemetry Service, which gained `telemetry.received`, the alarm route and snapshot retention holds;
- Web BFF, with the alarm routes.

So the run includes the checks covering those. The Phase 1 and 2 gates restart NATS and break a call on purpose, and
the mutual TLS check renews a certificate, so they run after the fault has streamed.

| Check | Proves | Result |
| --- | --- | --- |
| `fault-case-check` | A fault creates one deduplicated case bound to an immutable snapshot | Passed, run on its own |
| `edge-check` | The gateway serves only HTTPS, and only Keycloak's sign-in pages | Passed in the gate run |
| `alarm-check` | A device raises an alarm, people act on it, the device clears it | Passed in the gate run |
| `fault-rules-check` | Rules versioned, audited and changed only by a person | Passed in the gate run |
| `detection-check` | The detector finds, freezes its evidence, publishes and closes | Passed in the gate run |
| `case-check` | Two rules make one case, with evidence; cancelled; a person's own | Passed in the gate run |
| `telemetry-check` | Device credentials, the device route and storage | Passed in the gate run |
| `snapshot-check` | Telemetry read back, and frozen into held snapshots | Passed in the gate run |
| `authorization-check` | Roles, scopes, break-glass and audited denials | Passed in the gate run |
| `audit-check` | A tamper-evident audit record | Passed in the gate run |
| `cross-tenant-check` | 10 tables and 20 API attempts across tenants, all refused | Passed, run on its own |
| `security-check` | The Security dashboard and alerts, from real refusals | Passed, run on its own |
| `route-sweep` | Every route, the alarm screens' too, refuses who it must | Passed in the gate run |
| `bff-check` | Web BFF with a real login through the gateway | Passed in the gate run |
| `frontend-check` | Every screen in a real browser, at phone and desktop sizes, the alarm console too | Passed in the gate run |
| `mtls-check` | Mutual TLS, with a real certificate renewal | Passed in the gate run |
| `phase-1-gate` | A traced call through the gateway, and its breaker opening and recovering | Passed, run on its own |
| `phase-2-gate` | An event survives a NATS restart, and redelivery makes one record | Passed in the gate run |
| `make check`: lint, types, contracts and 1,620 tests; frontend; dependency audits | | Passed |

The Phase 3 checks not run again cover what Phase 5 did not touch: Keycloak and MFA set-up, data store certificate
renewals, token exchange, and the administrator and Platform consoles.

## What the gate found on the way

| Found | Fixed |
| --- | --- |
| The Authorization Service was killed for memory. It uses about 155Mi, its limit was 192Mi, and each check grants roles by running `bootstrap` in its pod, a second process. Two checks doing so at once were too much | Its request is 160Mi, as measured, and its limit 384Mi, room for two such processes |
| Docker Desktop's VM disk, shared by Minikube's node and the Mac's own Docker, was 100% full of build cache. Loki stops writing above 90%, answering "Ingester is shutting down", so Fluent Bit never became ready. The Phase 1 gate timed out waiting for it, and the cross-tenant check failed | Build cache and untagged images removed, about 13 GB, leaving the disk at 65%. Check the node's disk before long runs |
| The fault case's progress poll read an empty result as a rule. An empty result's last line is the tenant setting's | The query answers `none` when nothing has joined yet |
| After a cluster restart, the gateway's first rate-limited requests create its 429 series, so Prometheus sees no increase. The Security panel for them was empty once | None needed: the series exist from then on, and the check passed on its next run |

## Next

Phase 6, deterministic decisions, starts with M6.1, the Policy Service.
