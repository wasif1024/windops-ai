# ADR 0035: Changes to who may approve need a second administrator

**Status:** Accepted, 2026-11-21, for M10.2b.

## Context

**RBAC-007:** changes to approval policy shall need a second Tenant Administrator, or an escalation path where the
tenant has only one. SEC-003 already requires a recent second factor for approval-policy changes.

On 2026-11-21 the user agreed what counts as approval policy (ADR 0033):
- **Which roles hold `maintenance.approve`.** That is the tenant's role-permission mapping, now kept in versions.
- **Who holds those roles.** That is their grants.

There is no separate policy record. Until now, a Tenant Administrator changed both alone, and the change applied at
once.

## Decision

**A change to who may approve is held, not applied.** The Authorization Service holds:
- `GrantRole` or `RevokeRole` for a role that holds `maintenance.approve` in the tenant's mapping;
- `SetRolePermissions` that gives a role `maintenance.approve` or takes it away.

What happens instead:
- **The answer:** the call returns a pending change, with what it would do in words, rather than a grant or new
  permissions. Its proposer still needs the call's own permission and a recent second factor.
- **Other changes:** any change that does not touch who may approve applies at once, as before.
- **Audit:** the proposal is audited as `authorization.approval_policy.proposed`.

**A second person confirms it** (`ConfirmChange`):
- **Never its proposer.** The attempt is refused and audited as a denial.
- **A second Tenant Administrator:** another user in the tenant, holding the change's own permission
  (`roles.manage` for a grant or revocation, `permissions.manage` for a mapping), with a recent second factor.
- **Or the escalation, where there is no other:** if the tenant has no other tenant-wide Tenant Administrator,
  a Platform Administrator signed in to the platform tenant confirms it, with a recent second factor.
  - **Refusal:** a Platform Administrator is refused for any tenant that has a second administrator.
  - **Marking:** a change is marked `escalated` when proposed.
  - **Finding them:** Platform Administrators list the escalated changes across tenants.

**What confirming does:**
- **It applies the original request.** The rules are checked again then: the role must not already be held,
  and a tenant must not be left without anyone to manage roles.
- **It is recorded as the proposer's change.** A grant's `granted_by` is the proposer, so RBAC-006 still keeps
  them from deciding an approval requested before it. The confirmer is recorded on the change.
- **Audit:** it is audited as `authorization.approval_policy.confirmed`.

**Declining.** `DeclineChange` needs a reason. Its proposer may withdraw the change, or anyone who could confirm it
may refuse it. It is audited as `authorization.approval_policy.declined`. A pending change expires after 7 days.

**The bootstrap command** still grants without a second person. It is run inside the cluster, before anyone can
manage roles, and is audited as `bootstrap` (M3.6).

**The screens:**
- **Answering 202:** Web BFF answers 202 with the held change on the existing grant, revoke and role-permission
  routes.
- **New routes:** `/admin/policy-changes` and `/platform/policy-changes` list, confirm and decline.
- **Administration:** a new tab, Approval policy changes. The user and role pages say when a change is waiting.
- **The Platform console:** lists escalated changes.

## Consequences

- **Every tenant needs a second administrator, or a platform's attention,** to change who may approve. The
  tenant's other changes are unaffected.
- **The cluster checks:** they grant Maintenance Managers through the bootstrap command, or confirm with a second
  administrator, as `make approval-check` now does.
- **Re-checked when applied:** a pending change is applied against the tenant as it is when confirmed. If the role
  no longer holds `maintenance.approve` by then, it is still applied as proposed.
