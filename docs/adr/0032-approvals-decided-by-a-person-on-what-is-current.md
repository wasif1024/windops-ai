# ADR 0032: Approvals decided by a person, rechecked on what is current, failing closed

**Status:** Accepted, 2026-11-21, for M10.1.

## Context

SRS 4.8 and 8.5 give the Approval Service five requirements:
- **FR-APR-001:** no agent identity may approve a proposal.
- **FR-APR-002:** record the approver, decision, time, policy context, proposal version and evidence snapshot.
- **FR-APR-003:** reject an approval against a superseded proposal or snapshot.
- **EVD-009:** recheck the current snapshot and proposal version when a decision arrives.
- **EVD-010:** if a dependency this check needs is unavailable, the approval fails closed.

Other decisions already in place:
- **The permission:** `maintenance.approve` belongs to the Maintenance Manager (SRS 3.2). The Authorization Service
  holds every identity with an agent behind it to reads and `maintenance.draft` (ADR 0019).
- **State ownership (SRS 5.5):** the Maintenance Service owns the work order, and the Approval Service owns the
  decision.
- **What the Maintenance Service announces:** `workorder.submitted` once a person submits a validated work order
  (ADR 0029).
- **Later modules:** separation of duties is M10.2, and the SRS 8.5 stale-evidence rules with new versions are
  M10.3.

## Decision

**A submission opens a request.** The Approval Service consumes `workorder.submitted` and opens one `pending`
request for that work order's proposal version.
- **What the event carries:** it now carries what a request needs, so nothing has to be asked as a service: the
  case, turbine, evidence snapshot, submitter, and who drafted it, an agent included. The new fields only add to
  the event.
- **Repeats:** a repeated event opens nothing. An older version arriving late opens nothing either.
- **A new version:** it supersedes the earlier request that is still pending.

**Only a person decides (FR-APR-001, SEC-009).** `DecideApproval` refuses any identity with an agent behind it,
or with no person, before it asks Authorization anything.
- **Why before Authorization:** no change to the permission table could ever let an agent or a service decide.
- **Audit:** the refusal is audited as `authorization.denied`, with the agent as the actor on its principal's
  behalf, so the Denials page shows it.
- **Then Authorization:** `maintenance.approve` is checked for the turbine, its wind farm or the tenant, like the
  Maintenance Service's own checks.

**The decider names what they reviewed.** A decision states the proposal version and evidence snapshot the
decider saw. If either differs from the request, the decision is refused and nothing changes, so nobody decides on
something they did not see. A rejection needs a reason.

**Rechecked on what is current (EVD-009, FR-APR-003).** The work order and its case are read again from their
owners, the Maintenance and Investigation services, as the decider. The decision goes ahead only if all of these
hold:
- the work order is still `submitted`, at the request's version and on its snapshot;
- the case is still active (`open`, `investigating` or `pending_review`);
- the case's newest evidence snapshot is still the request's snapshot;
- the work order's policy result allows a decision (`pass` or `escalate`).

**What a failed recheck does:**
- **A moved proposal or snapshot:** the request becomes `superseded`, kept with the reason, and can never be
  decided. A new version is a new request (M10.3).
- **A policy result that allows no decision:** the decision is refused, but the request is not superseded, since
  the result can change.

**Failing closed (EVD-010).** Anything that cannot be read refuses the decision with `UNAVAILABLE`.
- **What it covers:** the work order, the case, Authorization, and the Asset Service for the turbine's wind farm.
- **What stays the same:** nothing is decided or announced, and the request stays `pending`.
- **Audit:** the refusal is audited as `approval.refused`, with the outcome `failed`.

**The record (FR-APR-002).** A decision records:
- its outcome, the person who decided, the time and the reason;
- the proposal version and evidence snapshot it was made on;
- the policy context: the Policy Service result and outcome from the work order's latest validation, and when
  that ran;
- what the recheck found: the case's status and evidence version, and the work order's revision.

A database trigger keeps a decided or superseded request unchanged: no update and no delete, even by the service's
own login. Only a `pending` request changes, and only once. Two managers deciding at the same moment get one
decision and one refusal.

**Announced with the decision (EVT-002, EVT-004).** `approval.decided` and the audit request go into the outbox in
the transaction that records the decision. The call returns only after that commits.
- **What the event carries:** the approval, work order, proposal version, snapshot, outcome and decider.
- **The reason stays out:** it is not in the event, so Notification never carries the decider's words.

**The Maintenance Service applies it.** It consumes `approval.decided` and moves the work order to `approved` or
`rejected`.
- **History:** the change goes into the work order's history and audit with the decider as the actor.
- **Only the current version:** it applies only to the version still `submitted`, so a late or repeated event
  changes nothing.
- **Only the Approval Service:** an event from any other producer is dead-lettered.

**What the Approval Service calls:** Maintenance, Investigation, Asset and Authorization.
- **No Policy call:** it does not call the Policy Service again, because it reads the policy result the work
  order already holds.
- **Each owner decides for the caller:** every call carries the decider's identity.
- **Breakers:** each call goes through its own breaker.

## Consequences

- **What M10.1 leaves for later:**
  - **M10.2:** separation of duties, so that nobody approves what they drafted, submitted or edited (RBAC-004 to
    RBAC-008), and the approval queue screen with its Web BFF routes. The fields that rule needs are already on
    each request.
  - **M10.3:** a stale proposal shows which SRS 8.5 rule fired, and is revised as a new version.
  - **M10.4:** the case resumes from `approval.decided`.
- **Reserving parts on approval** still needs its own fixed grant (ADR 0030), and is not done here.
- **Superseded snapshots:** freezing a case's evidence again supersedes its pending requests. That is the intended
  outcome under FR-APR-003, and M10.3 makes revising a superseded request straightforward.
- **Tests:** `make approval-check` proves all of this with the real services. It stops the Maintenance Service to
  show a decision failing closed, then restarts it.
