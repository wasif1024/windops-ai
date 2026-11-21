# windops-approval

Human decisions on submitted work orders (SRS 4.8, FR-APR-001, FR-APR-002, FR-APR-003). A Maintenance Manager
approves or rejects; no agent and no service ever decides
([ADR 0032](../../docs/adr/0032-approvals-decided-by-a-person-on-what-is-current.md)).

## A request

| Status | How it gets there |
| --- | --- |
| `pending` | `workorder.submitted` from the Maintenance Service opens one for that proposal version. A repeat opens nothing |
| `approved`, `rejected` | `DecideApproval`, by a person with `maintenance.approve`, once rechecked. Announced as `approval.decided` |
| `superseded` | The recheck found the proposal or snapshot moved on, or a newer version was submitted. Never decided |

A decided or superseded request never changes again: a database trigger refuses it.

## The recheck (EVD-009, EVD-010)

When a decision arrives, the work order and its case are read again from their owners, as the decider.

| Refused when | Then |
| --- | --- |
| The decider names a version or snapshot that is not the request's | Nothing changes: read it again |
| The work order is stale, or no longer submitted, or at another version or snapshot | Superseded, with the stale rule that fired (EVD-007) |
| The case's evidence is stale, or it is no longer active, or its newest snapshot is another | Superseded |
| The work order's policy result is neither `pass` nor `escalate` | Refused; still pending |
| Maintenance, Investigation, Asset or Authorization cannot be asked | `UNAVAILABLE`; still pending |

## The record (FR-APR-002)

Each decision records its outcome, the person who decided, the time and the reason. It also records the proposal
version and snapshot it was made on, and the policy context: the Policy Service result, what the case was, and
the work order's revision. `approval.decided` and the audit request go into the outbox in the same transaction,
and the call returns only once it commits (EVT-004). The Maintenance Service applies the decision to its work
order.

## Separation of duties (RBAC-004 to RBAC-006, RBAC-008)

[ADR 0033](../../docs/adr/0033-separation-of-duties-at-decision-time.md):
- **Requesters never decide:** the work order's history is read as the decider. Whoever drafted, changed or
  submitted it, or the person an agent drafted it for, is refused, and the request stays pending for another
  manager.
- **Eligibility now, not by your own change:** `maintenance.approve` is asked of Authorization as of the
  submission, leaving out roles the decider gave themselves since and their own role-permission changes.
- **The record:** each decision lists who was excluded, the rules (`separation-of-duties-2`) and the
  role-permission version.

## Agents never decide (FR-APR-001, SEC-009)

This service refuses any identity with an agent behind it, or with no person, before it asks Authorization. Each
refusal is audited as `authorization.denied`, with the agent as the actor on its principal's behalf.

## Run the tests

```bash
uv run pytest services/approval-service/tests
make approval-check   # on the cluster: people decide, agents never, requesters refused, superseded refused, failing closed
```
