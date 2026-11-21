# ADR 0033: Separation of duties, checked at decision time

**Status:** Accepted, 2026-11-21, for M10.2.

## Context

SRS 3.3 gives approvals five access rules:
- **RBAC-004:** nobody may approve a request they drafted, submitted or changed.
- **RBAC-005:** the Approval Service checks eligibility against the policy version and roles active at decision
  time.
- **RBAC-006:** a role or policy change shall not make its author eligible for a request created before the change.
- **RBAC-007:** changes to approval policy need a second Tenant Administrator, or an escalation path where the
  tenant has only one.
- **RBAC-008:** for an agent-prepared draft, the requester is the person who started the investigation and any
  person who submits or edits the draft. A fault-triggered draft has no eligible agent approver.

Already in place:
- **ADR 0032:** the Approval Service refuses agents and services before anything else. It rechecks each request
  against the current work order and case.
- **The work order's history:** the Maintenance Service keeps every draft, change and submission, with the person
  behind it. For an agent's change, that is the agent's principal (ADR 0029).
- **Who an agent acts for:** an investigation a person starts gives its agents that person's delegation, so an
  agent's draft is recorded as drafted for them. A fault-triggered investigation acts for the tenant's
  `service:fault-investigator` (ADR 0019).
- **Who may approve:** only `maintenance.approve` decides that, through which roles hold it and who holds those
  roles. Grants are ended, never deleted (M3.6).

With the user, on 2026-11-21:
- **RBAC-007 moves to its own module,** M10.2b, because the two-person rule needs its own workflow and screens.
- **The approval policy is the role permissions.** A change to which roles hold `maintenance.approve` is a change
  to approval policy, and a grant of a role is a role change. No separate policy record is added.

## Decision

**The requesters come from the work order's history, read at decision time (RBAC-004, RBAC-008).** The Approval
Service already reads the work order as the decider. It now reads the work order's history too.
- **Who counts as a requester:** whoever is named as having drafted, changed or submitted it, together with the
  request's drafter and submitter.
- **Validating does not count,** because it changes nothing in the plan.
- **An agent's draft:** its recorded actor is its principal. So the person who started the investigation is a
  requester, as is every person who edited or submitted the draft.
- **A fault-triggered draft:** its principal is a service, which can never decide anyway (SEC-009).
- **What a requester gets:** a requester who tries to decide is refused with `PERMISSION_DENIED`, and the refusal is
  audited as `approval.refused`. The request stays pending for another manager.
- **The record:** each decision lists the requesters who were excluded, and the rules it was held to, named
  `separation-of-duties-1`. A change to these rules is a new name.

**Eligibility is decided now, with the decider's own later changes left out (RBAC-005, RBAC-006).**
- **What Authorization is asked:** the Approval Service asks for `maintenance.approve` as of the request's
  submission time (`eligible_since`). It asks tenant-wide first, then for the turbine's wind farm, as before.
- **Grants:** the Authorization Service decides with the grants active now (RBAC-005). It then decides again
  without the grants the caller gave themselves after `eligible_since`.
- **Role permissions:** in that second decision, any role whose permissions the caller changed after
  `eligible_since` is set back to what it was before their first such change.
- **The result:** allowed only if both decisions allow it. Changes by other people still count, as does anything
  the caller changed before the request.
- **Versions:** each role-permission change is now kept as a new version, with its author, its time and what it
  replaced, and a trigger keeps that record unchanged. `Check` returns the version it decided on: 0 for the SRS 3.2
  defaults. The decision records that version beside the rules' name.

**The approval queue screen.** Web BFF adds three routes: `GET /approvals`, `GET /approvals/{id}` and
`POST /approvals/{id}/decision`. They hold no rules of their own.
- **The queue:** lists requests oldest first, by status, marking an agent's draft.
- **A request:** shows the work order's plan, its validation and its history, so the manager reviews what they
  decide on.
- **The decision:** made on exactly the version and snapshot shown. Once made, the screen shows the record, with
  who could not decide.
- **Refusals:** the BFF never forwards a service's words. A refusal under RBAC-004 or RBAC-006 is sent as a code
  (`requester` or `own_change`), which the screen explains in its own words.

## Consequences

- **RBAC-007 is M10.2b:** a second Tenant Administrator confirms a change that adds or removes
  `maintenance.approve` from a role, or a grant of a role that holds it, with an escalation when the tenant has
  only one. The versions kept here are what it builds on.
- **Changes from before this module:** role-permission changes made before it have no recorded versions. A tenant
  that was already customized starts at version 0 for those changes, and cannot have them undone for RBAC-006.
- **Being careful by design:** if the decider changed a role after the request, it is judged as it was before
  their change, even if someone else changed it again later. This can refuse someone who would be eligible
  without their own change, but it never lets a change of theirs count.
- **Asking for history:** reading the history means the Maintenance Service must answer for the decision to go
  ahead. That was already true: nothing is decided without it (EVD-010).
