# Roles and permissions

Who may do what in WindOps, and how it is decided (SRS 3, SEC-006). The Authorization Service decides every action,
and anything not granted is denied. How sign-in and the rest of security work is in [security.md](security.md).

## Roles

| Role | Short | For |
| --- | --- | --- |
| Platform Administrator | PA | WindOps staff. Lives in the `windops-platform` tenant, and manages tenants |
| Tenant Administrator | TA | A customer's own administrator: users, roles, permissions, branding and policies |
| Operations Manager | OM | Runs operations, and reviews investigations and findings |
| Control Room Operator | CO | Watches alarms and turbines, and acknowledges alarms |
| Reliability Engineer | RE | Investigates faults and reviews findings |
| Maintenance Planner | MP | Drafts and submits work orders, and manages parts |
| Maintenance Manager | MM | Approves work orders |
| Auditor | AU | Reads the audit trail and decisions, and changes nothing |

## The default matrix (SRS 3.2)

This table is written from the Authorization Service's policy, and `tests/test_rbac_doc.py` keeps the two the same.

| Permission | PA | TA | OM | CO | RE | MP | MM | AU |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `turbine.read` |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `asset.manage` |  | ✓ |  |  |  |  |  |  |
| `telemetry.read` |  |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `alarm.read` |  |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `alarm.acknowledge` |  |  | ✓ | ✓ |  |  |  |  |
| `investigation.create` |  |  | ✓ | ✓ | ✓ |  |  |  |
| `investigation.read` |  |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `investigation.cancel` |  |  | ✓ |  | ✓ |  |  |  |
| `finding.review` |  |  | ✓ |  | ✓ |  |  |  |
| `maintenance.draft` |  |  |  |  |  | ✓ |  |  |
| `maintenance.submit` |  |  |  |  |  | ✓ |  |  |
| `maintenance.approve` |  |  |  |  |  |  | ✓ |  |
| `maintenance.complete` |  |  |  |  |  | ✓ | ✓ |  |
| `inventory.read` |  |  | ✓ |  | ✓ | ✓ | ✓ | ✓ |
| `inventory.update` |  |  |  |  |  | ✓ |  |  |
| `inventory.reserve` |  |  |  |  |  | ✓ |  |  |
| `weather.read` |  |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `knowledge.read` |  |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `knowledge.manage` |  | ✓ |  |  | ✓ |  |  |  |
| `policy.read` |  | ✓ | ✓ |  | ✓ |  | ✓ | ✓ |
| `policy.manage` |  | ✓ |  |  |  |  |  |  |
| `users.manage` | ✓ | ✓ |  |  |  |  |  |  |
| `roles.manage` | ✓ | ✓ |  |  |  |  |  |  |
| `permissions.manage` | ✓ | ✓ |  |  |  |  |  |  |
| `tenant.manage` (platform only) | ✓ |  |  |  |  |  |  |  |
| `branding.manage` |  | ✓ |  |  |  |  |  |  |
| `audit.read` | ✓ | ✓ |  |  |  |  |  | ✓ |
| `gateway.configure` (platform only) | ✓ |  |  |  |  |  |  |  |
| `mcp.configure` | ✓ | ✓ |  |  |  |  |  |  |
| `evaluation.manage` (platform only) | ✓ |  |  |  |  |  |  |  |

`policy.read` and `policy.manage` also cover the Fault Detection Service's fault rules, which are thresholds like the
Policy Service's. A change to one also needs a recent second factor, so only a person can make it, never an agent
([windops-fault-detection](../services/fault-detection-service/README.md#versions-and-who-may-change-them-fr-flt-002-fr-flt-005)).

A tenant's Tenant Administrator may change what each role gives in that tenant, from the administrator console's
Role permissions tab. These rules still hold:

- **Platform-only permissions** (`tenant.manage`, `gateway.configure`, `evaluation.manage`) cannot be given to a
  tenant's role.
- **The Tenant Administrator keeps** `roles.manage` and `permissions.manage`, or nobody could manage roles again.
- **The Platform Administrator role** cannot be changed by a tenant, exists only in the platform tenant, and cannot be
  scoped.
- **A tenant always keeps one Tenant Administrator.** The last one cannot be revoked.

## How a decision is made

1. **Who and which tenant** come from the caller's verified identity, never from the request (RBAC-001).
2. **In their own tenant,** the caller's active grants are mapped to permissions, using the tenant's own role
   mapping if it changed one, or the default above.
3. **The scope must cover the resource** (RBAC-002). A grant is for the whole tenant, a wind farm, or one turbine in a
   wind farm. A wind farm grant covers its turbines.
4. **In another tenant,** only a Platform Administrator may act, and only for administration: `users.manage`,
   `roles.manage`, `permissions.manage`, `tenant.manage` and `mcp.configure`. Reading the tenant's data needs
   break-glass.
5. **Anything else is denied**, and the denial is recorded in the caller's own tenant's audit trail.

## Changes need a second factor

Giving and revoking roles, and changing what a role gives, need a second factor from the last 5 minutes (SEC-003). So
do the Identity and Tenant changes in the consoles. Without one, the console asks for a code and the change is made
again.

## Break-glass (RBAC-003)

A Platform Administrator reaches a tenant's data only through break-glass, from the Platform console:

- a reason of at least 20 characters, kept with the grant, and recorded in that tenant's audit trail, so the tenant
  can see it,
- for 1 to 240 minutes (60 by default), ending by itself, or earlier by hand,
- with a second factor from the last 5 minutes,
- read-only: only `.read` permissions, never a change,
- every use recorded too (`authorization.break_glass.used`), and an alert when one starts,
- and only a Platform Administrator may start, list or end one: anyone else is refused, and the refusal recorded.

## Grants are kept

A revoked grant is ended, never deleted, so an approval can be checked against the roles someone held at the time
(RBAC-005). The administrator console shows ended roles under each user.

## Giving the first roles

Nobody can grant a role before someone holds one, so the first come from a command run inside the Authorization
Service's pod: `make authorization-seed` for the demo users, or by hand:

```bash
kubectl exec deployment/windops-authorization --namespace=windops-system -- \
  python -m windops_authorization.bootstrap --tenant north-sea --user <user id> --role tenant-administrator
```

It is safe to repeat, and recorded in the audit trail as by `bootstrap`.

## Still to come

- **Separation of duties for approvals** (RBAC-004 to RBAC-008) arrives with the Approval Service, in Phase 10.
- **Agent and service identities** are structurally ineligible to approve (SEC-009), also in Phase 10.
