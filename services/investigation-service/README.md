# windops-investigation

Investigation cases: what is being looked into on a turbine, and why (SRS 4.5, FR-INV-001, FR-FLT-004). It owns each
case's status, its detections and its history (SRS 5.5).

## How a case starts

| From | How |
| --- | --- |
| The detector | Each `fault.detected`, from the Fault Detection Service only, goes into a case by the case rules |
| A person | `CreateCase`, with `investigation.create`, for a turbine they may see. Always a case of its own |

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
| `investigating`, `pending_review` | Agent Runtime, in Phase 8 |
| `closed` | Approval and closure, in Phase 10 |
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

## Run the tests

```bash
uv run pytest services/investigation-service/tests
make case-check   # on the cluster: one case from two rules, its evidence frozen and verified, and who may do what
make fault-case-check  # the Phase 5 gate: a seeded gearbox fault, found by the default rules, makes one case
```
