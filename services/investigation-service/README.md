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

Every change is kept in the case's history with who made it, announced as `case.updated` and audited. Reading needs
`investigation.read` for the turbine, its wind farm or the tenant, with the wind farm from the Asset Service, asked as
the caller. A case the caller may not read is not found. M5.5 binds each case to an immutable evidence snapshot.

## Run the tests

```bash
uv run pytest services/investigation-service/tests
make case-check   # on the cluster: two rules' detections make one case, cancelled, and a person's own
```
