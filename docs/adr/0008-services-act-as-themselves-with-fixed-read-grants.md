# ADR 0008: Services act as themselves, with fixed read-only grants

**Status:** Accepted, 2026-11-13

## Context

Until now, every call between services carried a person's identity: a service asked another as the user it was
serving, and the Authorization Service denied any identity without a user. The Fault Detection Service's detector
(FR-FLT-003) has no user. It must read each streaming turbine's telemetry and alarms on its own schedule, and freeze
a telemetry snapshot for each detection. SEC-009 and SEC-010 already speak of service principals: they must never be
able to approve, and audit records must name them.

## Decision

A service acting as itself signs an internal identity whose subject is `service:<name>`, with no user, in the tenant
it acts for. The Authorization Service allows such an identity only what a fixed table in its code grants that
service:

| Service | Grants |
| --- | --- |
| `fault-detection` | `turbine.read`, `telemetry.read`, `alarm.read` |

Every grant is tenant-wide, and only in the tenant the identity names. A service is never allowed across tenants,
never a `.manage` permission, and never an approval. A test fails if a grant that is not `.read` is ever added. A
tenant administrator cannot grant a service more, because service grants are not roles.

Audit records name the service as the actor, such as `service:fault-detection`, as they name a user.

## Consequences

- The detector reads what it needs without borrowing anyone's identity, and every service it calls still asks
  Authorization, so no service is trusted to skip the check.
- Like a user's, a service identity is signed with the shared internal key, so its trust rests on the same secret.
  Binding a service's identity to its mTLS certificate is a later hardening step, for M14.
- A new service that needs to act alone adds a row to the table, in code, with review.
