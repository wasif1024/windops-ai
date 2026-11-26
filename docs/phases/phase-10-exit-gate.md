# Phase 10 exit gate

**Result:** passed on 2026-11-26, on the local Minikube cluster, at no cost. Every agent run was rehearsed, and email
was only recorded (ADR 0037).

SRS section 12 sets the gate: *the full flow passes from fault to verification, with 0 approval bypasses.*

```bash
make full-flow-check   # the gate itself, about 6 minutes
make phase-10-gate     # the gate, then every Phase 10 check and the earlier ones Phase 10 touched, about 40 minutes
```

## The gate itself

`make full-flow-check` runs the whole flow on a turbine of its own, in the `asset-check` tenant. It acts as the
turbine's device, sending its telemetry through the gateway a sample a second. At each step it tries every way around
the approval:

```text
ok - The seeded fault, gearbox oil at 85 °C, is detected by gearbox-oil-critical and opens critical case case_5dc60042f1484b38b279
ok - Its evidence is frozen (evs_f63e1b95576e42c491ac) and the investigation, run run_nn7e2oxdxkcjryqskuso, waits for review (SRS 5.4 step 13)
ok - The planner submits work order wo_ec27d6154c7f80e4be5b; neither the agent nor the fault-investigator may
ok - 8 ways around the approval are refused, and the request still waits for a decision
ok - The manager approves it on snapshot evs_f63e1b95576e42c491ac; the decision resumes the case's workflow, and the case is approved (SRS 5.4 step 14)
ok - The oil cools to 50 °C, and the planner records the work done (FR-MNT-004)
ok - After the window the work is verified resolved, as the rules give: gearbox oil 85.0 then 50.0 °C (SRS 5.4 step 15, FR-MNT-005)
ok - The engineer reads the outcome and closes the case
ok - 0 approval bypasses: all 9 attempts were refused
Full flow check passed
```

**The flow:**
- **The fault:** the default library's `gearbox-oil-critical` rule found it, not a rule made for the check.
- **The investigation:** it ran as the tenant's fault-investigator, rehearsed, and its planner agent drafted a work order
  in step 12 (ADR 0031).
- **The proposal:** the planner, a person, re-planned that draft and submitted it.
- **The decision:** a manager with no hand in the work approved it on the snapshot under review, and the decision
  resumed the case's workflow (ADR 0036).
- **The work:** the planner recorded it done. After the two-minute local window, the verification froze the telemetry
  after the work and ran the fault rules again. It reported resolved, which the check confirmed by asking the rules
  itself (ADR 0038).
- **The close:** the engineer who reviews findings closed the case.

**The 9 attempts, each refused:**

| Attempt | Refused with | Rule |
| --- | --- | --- |
| The planner's agent submits the work order | `PERMISSION_DENIED` | SEC-009 |
| The fault-investigator principal submits it | `PERMISSION_DENIED` | SEC-009 |
| The manager's agent approves | `PERMISSION_DENIED` | FR-APR-001 |
| The fault-investigator approves | `PERMISSION_DENIED` | FR-APR-001 |
| The planner who submitted it, who is also a manager, approves | `PERMISSION_DENIED` | RBAC-004 |
| A decision naming another proposal version | `FAILED_PRECONDITION` | EVD-009 |
| A decision naming another evidence snapshot | `FAILED_PRECONDITION` | EVD-009 |
| The planner completes the work before it is approved | `FAILED_PRECONDITION` | SRS 4.12 |
| The planner's agent completes the work | `PERMISSION_DENIED` | SEC-009 |

## Every check

`make phase-10-gate` runs the gate, then every Phase 10 check and the earlier checks covering what Phase 10 changed.

| Check | Proves |
| --- | --- |
| `full-flow-check` | A fault to verification, with 0 approval bypasses |
| `approval-check` | Only a person decides, on what is current, apart from their own work, failing closed (M10.1, M10.2) |
| `policy-change-check` | A change to who may approve waits for a second administrator (M10.2b) |
| `stale-check` | Versioned stale rules, shown at approval, re-evaluated as new versions (M10.3) |
| `stale-watch-check` | Condition drift and weather, polled (M10.3b) |
| `resume-check` | A decision resumes the case's workflow from its checkpoint (M10.4) |
| `notification-check` | Who is told what, in the app and by recorded email, with no evidence (M10.5) |
| `verification-check` | Completed work verified by the rules, and a person closes the case (M10.6) |
| `maintenance-check` | Work orders validated, drafted by anyone, submitted by a person |
| `agent-denial-check` | An agent drafts, and never submits or reserves (the Phase 9 gate) |
| `frontend-check` | Every console journey, the approval queue included, on a phone too |
| `bff-check` | Web BFF's routes, notifications included |
| `authorization-check` | Roles, scopes, service grants and audited denials |
| `cross-tenant-check` | Every service's database and API attacked across tenants: 143 tables and 20 API attempts |
| `audit-check` | A tamper-evident audit record |
| `migrate-check` | Migration Jobs run once per release |
| `nats-check` | JetStream keeps events and limits each publisher |

All 17 passed, across two runs of the gate on the same code:

- **2026-11-25, 20:45 to 21:04:** `policy-change-check`, `stale-watch-check`, `resume-check`, `notification-check`,
  `verification-check`, `maintenance-check` and `agent-denial-check` passed. The run then stopped when the laptop
  slept.
- **2026-11-26, 06:42 to 07:03:** the other ten passed, in 21 minutes, named to the gate:
  `uv run python -m scripts.checks.gates.phase_10 full-flow-check approval-check ...`.

## What the gate found

The gate's code needed small fixes, and the local cluster needed more CPU for its busiest services. None of this
changed what Phase 10 does.

**Fixes to the checks:**
- **`nats-check`** compared the stream's message count before and after its forbidden attempts. Other services
  publish, and old events expire, meanwhile. It now compares sequences: a forged event would land after the old last
  sequence, and a purge would start the stream after it. It also runs last now, as it restarts NATS.
- **The Keycloak admin client** kept one token, and admin-cli tokens last a minute. A check slowed by load outlived
  it and got a 401. It now takes a new token when Keycloak refuses one, with a unit test.
- **`stale-watch-check`** now cancels any case left on its turbine by an earlier run.
- **`make phase-10-gate`:**
  - before each check, it waits up to ten minutes for every pod to be ready and the node's load to fall below 24;
  - it accepts the names of the checks to run.

**The cluster's CPU:** at first, three runs of the gate failed part-way. Services were restarted by their own health
probes, and every check running at the time failed with them. The cause was CPU limits set early in the project, now
too low for the work:

| Service | Measured | Limit |
| --- | --- | --- |
| Authorization | Throttled in 1 period of 10 at rest. Each check's role grants run a second process inside it | 250m to 500m |
| Telemetry | Throttled in a third of its periods, which tripped Web BFF's breaker. Evidence, verification and drift now read it too | 500m to 1000m |
| Fault Detection | Throttled in 1 period of 5. Drift and verification now run its rules too | 500m to 750m |

**What did not change:** requests are unchanged, so the 16 GB laptop budget (ADR 0001) is still met.

**During the gate's runs:**
- **The observability stack was paused:** Grafana, Prometheus, Loki, Tempo, Alertmanager and kube-state-metrics.
  Nothing in the gate reads them, and they were scaled back to one replica each afterwards.
- **Minikube was restarted once**, after PostgreSQL was evicted under memory pressure.

**Revisit:** the probes' one-second timeout leaves little room on a busy six-CPU node. Phase 11 adds browser-heavy
checks, so measure the node before its gate.
