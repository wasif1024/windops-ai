# windops-investigation

Investigation cases: what is being looked into on a turbine, and why (SRS 4.5, FR-INV-001, FR-FLT-004). It owns each
case's status, its detections and its history (SRS 5.5).

## How a case starts

| From | How |
| --- | --- |
| The detector | Each `fault.detected`, from the Fault Detection Service only, goes into a case by the case rules |
| A person | `CreateCase`, with `investigation.create`, for a turbine they may see. Always a case of its own. With `investigate`, it is investigated too |

## One fault, one case (FR-FLT-004)

The case rules, `case-rules-1` in `case_rules.py`, give each fault rule a component group: the gearbox's rules group
as `gearbox`, the main bearing's as `main_bearing`, vibration's as `vibration`, missing readings as `sensors`, and a
tenant's own rules as `turbine`.

| A detection arrives, and the turbine's component | It |
| --- | --- |
| Has an active case from detections | Joins it, raising the case's severity if it is higher |
| Has none, and the detection is medium or above | Opens a new case |
| Has none, and the detection is low | Opens nothing: a short data gap is not a fault |

A case's severity is the highest of its detections', worked out, never judged. Each detection is kept once, by its
ID, so `fault.detected` sent twice changes nothing. Each case records the case rules that grouped it.

## States

| Status | Set by |
| --- | --- |
| `open` | Created, waiting for its investigation |
| `investigating` | This service, when the case's investigation starts |
| `pending_review` | This service, when its run completes (SRS 5.4 step 13). A run that ends otherwise puts the case back to `open` |
| `approved` | This service, when a person approves the case's work order and the decision run resuming the investigation completes (SRS 5.4 step 14). A rejection leaves the case waiting for a revised proposal; a resume that cannot carry on puts it back to `open`, to restart from the snapshot ([ADR 0036](../../docs/adr/0036-step-14-a-decision-resumes-the-case-in-a-run-of-its-own.md)) |
| `verifying` | This service, when a person records the approved work done, from `workorder.completed` (SRS 5.4 step 15) |
| `verified` | This service, once the window after the work has passed: the telemetry after it is compared with the evidence, the fault rules run again, and the outcome reported, resolved, not resolved or inconclusive ([ADR 0038](../../docs/adr/0038-step-15-completed-work-verified-by-the-fault-rules.md)) |
| `closed` | A person with `finding.review` closing a verified case with `ReviewVerification`. Reopening it puts it back to `open` |
| `cancelled` | A person with `investigation.cancel`, with a reason |

## Evidence snapshots (FR-INV-002, EVD-001 to 004)

Every case is bound to evidence, frozen. A snapshot holds, each as it was when frozen:

| Item | From | Frozen as |
| --- | --- | --- |
| The turbine | Asset | Its record and version |
| The case's telemetry window | Telemetry | A telemetry snapshot of its own, hashed, with a retention hold, and its data quality |
| Every alarm on the turbine in the window | Alarm | Each alarm, with its version |
| Each detection | Fault Detection | Its record, with the telemetry snapshot it saw |
| Each detection's rule version | Fault Detection | Its exact definition and content hash |

Each item is canonical JSON with its own sha256. The snapshot's hash is sha256 over
`windops-evidence-snapshot-v1|<tenant>|<case>|<version>|<from>|<to>`, then a line an item: its source, type, ID,
version and hash. Reading a snapshot checks every hash again, and says whether it still verifies. A database trigger
refuses to change or delete one, even for this service's own login.

The window starts at the earliest detection's, and ends now, at most a day. A case's first snapshot is frozen as soon
as the case opens, and a new version each time a detection joins it. The builder does it as the service itself
([ADR 0008](../../docs/adr/0008-services-act-as-themselves-with-fixed-read-grants.md)), and a person with
`investigation.create` may freeze a new version on demand, as themselves. If any source cannot be reached there is no
snapshot, never a partial one, and the case's evidence stays `pending` until the next try (SRS 7.7).

Every change is kept in the case's history with who made it, announced as `case.updated` and audited. Reading needs
`investigation.read` for the turbine, its wind farm or the tenant, with the wind farm from the Asset Service, asked as
the caller. A case the caller may not read is not found.

## Stale evidence (SRS 8.5, EVD-007, EVD-008)

The rules, `stale-rules-2` in `staleness.py`, judge the case's newest snapshot and the proposals submitted on it.
The first to fire marks the snapshot stale, with the rule, what it found and when, announced on `case.updated`:

| Rule | Fires when | From |
| --- | --- | --- |
| severity | A detection raises the severity once evidence is frozen | `fault.detected` |
| alarm | A high or critical alarm on the turbine becomes active, at a version the snapshot does not hold | `alarm.changed` |
| procedure | A document a submitted proposal cites is replaced | `evidence.changed` from Knowledge |
| asset | The turbine's status, wind farm, model or serial number changes | `evidence.changed` from Asset |
| inventory | A proposed part is no longer available | `evidence.changed` from Inventory |
| age | The snapshot passes 24 hours (critical, high), 72 (medium) or 168 (low) | A sweep every five minutes |
| drift | One of the case's fault rules, run again, finds a figure more than half away from the one its detection froze, and at least 1 | The watcher, every five minutes, asking Fault Detection |
| weather | Asked again, the Policy Service's work-window rules do worse for a submitted proposal with work at height than at submission: a newer forecast breaches the window, or it expired | The watcher, asking Policy at most once an hour for each proposal, or when those rules change |

`workorder.submitted` tells it what each proposal names. A new snapshot clears the mark: the case is re-evaluated
([ADR 0034](../../docs/adr/0034-stale-evidence-rules-and-new-versions.md)). The watcher judges a case only until its
work is done, and an owner it cannot reach only delays it
([ADR 0039](../../docs/adr/0039-drift-by-the-cases-fault-figures-and-weather-by-policy.md)).

## Investigations (API-003)

Each case's investigation runs in the [Agent Runtime](../agent-runtime-service/README.md), started and followed by
this service ([ADR 0025](../../docs/adr/0025-investigations-started-and-followed-by-their-case-owner.md)).

| Case | Investigated | Its agents act for |
| --- | --- | --- |
| From detections | Always, when it opens | The tenant's `fault-investigator` |
| A person's | When `CreateCase` asks with `investigate` and the person's own Keycloak token | That person, under a four-hour delegation |

- **Starting:** the person gets the case's ID at once. A keeper starts the run once the case's first evidence
  snapshot is frozen, and the case becomes `investigating`.
- **The token:** it is passed to the Identity Service for the delegation, then dropped: never stored or logged.
- **Following:** the keeper reads each run every two seconds and keeps each completed step as a numbered event. When
  the run ends, it moves the case. A cancelled case cancels its run.
- **Watching:** `WatchInvestigation` streams a case's events from any sequence, with `investigation.read`. Web BFF
  passes them on as server-sent events.
- **Cost:** `WINDOPS_INVESTIGATION_AGENTS` sets how runs go:
  - `rehearse`: a scripted model, at no cost. The default.
  - `stand_in`: a dry run of the real Claude client against a stand-in API, at no cost.
  - `claude`: the agents on Claude.
  - `off`: no runs.

  `WINDOPS_INVESTIGATION_COST_BUDGET_USD` may cap each run's cost below the Claude policy's.

## Findings and their reviews (FR-INV-006)

The findings are the investigation run's report (SRS 5.7), which the Agent Runtime keeps and only this service may
read, as itself, once the caller may read the case.
- **`GetInvestigationReport`:** the report, and every review of its findings, with `investigation.read`.
- **`ReviewFinding`:** a person with `finding.review`, never an agent, accepts, rejects, challenges or annotates one
  finding, named by where it is in the report, such as `hypotheses[0]`. Anything but accepting needs a note.
- **What a review does:** it is kept as made (a trigger refuses any change), audited, and in the case's history. It
  decides nothing: the approval queue shows the findings still challenged or rejected beside the work order, and the
  Maintenance Manager decides knowing them
  ([ADR 0040](../../docs/adr/0040-finding-reviews-recorded-and-shown-to-the-approver.md)).

## Run the tests

```bash
uv run pytest services/investigation-service/tests
make case-check   # on the cluster: one case from two rules, its evidence frozen and verified, and who may do what
make fault-case-check  # the Phase 5 gate: a seeded gearbox fault, found by the default rules, makes one case
make progress-check    # an investigation through the gateway, its progress streamed, dropped and resumed
```
