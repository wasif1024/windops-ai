# windops-maintenance

Maintenance plans and work orders (SRS 4.12, FR-MNT-002, FR-MNT-003). A person or an agent drafts a work order
against an active case; only a person submits it ([ADR 0029](../../docs/adr/0029-work-orders-drafted-by-anyone-submitted-by-a-person.md)).

## A work order

| State | How it gets there |
| --- | --- |
| `draft` | `CreateDraft`, with `maintenance.draft`, by a person or an agent. `UpdateDraft` replaces its plan, from the revision the caller saw |
| `submitted` | `SubmitWorkOrder`, with `maintenance.submit`, by a person only, once nothing blocks it. Announced as `workorder.submitted` |
| `withdrawn` | `WithdrawDraft`, by a person, with a reason |
| `approved`, `rejected` | A person's decision through the Approval Service, applied from its `approval.decided` to the version still submitted ([ADR 0032](../../docs/adr/0032-approvals-decided-by-a-person-on-what-is-current.md)) |

| `stale` | The Investigation Service's rules found its evidence snapshot stale, from its `case.updated`, with the rule kept ([ADR 0034](../../docs/adr/0034-stale-evidence-rules-and-new-versions.md)) |
| `completed` | `CompleteWorkOrder`, with `maintenance.complete`, by a person only, on approved work: when it started and ended, a summary, the parts used and the downtime (FR-MNT-004). Announced as `workorder.completed`, with identifiers and times only |
| `verified` | The Investigation Service's verification of the work, from its `case.updated`, with the outcome kept ([ADR 0038](../../docs/adr/0038-step-15-completed-work-verified-by-the-fault-rules.md)) |

A stale or rejected work order is revised with `ReviseWorkOrder` as its next version, a draft on the case's current
snapshot, once the case is re-evaluated. Each earlier version is kept as it was.

## Validation (FR-MNT-002)

Run as a draft is made or changed, on demand with `ValidateWorkOrder`, and again at submission. A blocking problem
stops submission; the rest are for the reviewer.

| Check | Blocks when |
| --- | --- |
| The plan | A title, action or summary is missing; there is no task; a task is outside 0 to 240 hours or 1 to 20 people; a part or procedure is malformed, or a part is named twice; a repair, replacement or adjustment cites no procedure; the window ends before it starts or has started; downtime is outside 0 to 720 hours |
| The turbine | It cannot be found, or its status cannot be checked. Faulted, stopped, offline or in maintenance is noted |
| The parts | A part is unknown, retired, or does not fit the turbine's model, or the Inventory Service cannot be asked. A part short in the wind farm's store is noted. Each part's availability is kept with the validation ([ADR 0030](../../docs/adr/0030-parts-checked-by-anyone-reserved-by-a-person.md)) |
| The Policy Service | Its rules block the plan, or it cannot be asked. An escalation is noted |

## Agents only draft (FR-MNT-003, SEC-009)

The Maintenance Planner agent drafts through WindOps MCP's `create_draft_work_order`, for its principal. This service
refuses any identity with an agent behind it, or with no person, before it submits or withdraws anything, whatever
the Authorization Service would allow. Each refusal is audited as `authorization.denied`, with the agent as the actor
on its principal's behalf, so the Denials page shows it.

## Run the tests

```bash
uv run pytest services/maintenance-service/tests
make maintenance-check   # on the cluster: drafts, validation, submission, and agents held to drafts
```
