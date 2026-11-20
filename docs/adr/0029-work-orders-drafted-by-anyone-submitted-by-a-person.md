# ADR 0029: Work orders drafted by people or agents, submitted only by a person

**Status:** Accepted, 2026-11-20, for M9.1.

## Context

SRS 4.12 sets a work order's lifecycle, through the Maintenance and Approval services: a draft, a submission, a
human decision, completion and verification. The requirements this module answers:
- **FR-MNT-002:** validate the asset's status, the plan's fields, its procedures and policy constraints.
- **FR-MNT-003:** agents create drafts only, with submission and approval controlled separately.
- **SEC-009:** an agent only reads and drafts, whoever it acts for.

The Authorization Service already holds every identity with an agent behind it to reading and drafting (ADR 0019),
and its permission table gives `maintenance.draft` and `maintenance.submit` to the Maintenance Planner role.

## Decision

**The Maintenance Service owns work orders,** each planned against an active case.
- **What a plan holds:** a title and action; a summary; tasks with their duration, crew and whether they need a climb;
  parts; the procedures it follows, by knowledge citation; a window; the expected downtime; and the production
  impact estimate, if any.
- **What a work order adds:** the case, the turbine, and the case's evidence snapshot when drafted. It has a proposal
  version, from 1 (Phase 10 adds new versions), and a revision that each change increments.
- **States:** `draft`, `submitted` and `withdrawn`. Phase 10 adds approval, rejection, staleness, completion and
  verification.

**Validated as drafted, and again at submission (FR-MNT-002).** Each check is kept with the work order, and a
blocking one stops submission:
- **The plan:**
  - a title, an action and a summary;
  - at least one task, each 0 to 240 hours with a crew of 1 to 20;
  - parts by number and quantity;
  - procedures cited as knowledge passages, with at least one for a repair, replacement or adjustment;
  - a window that ends after it starts, and starts in the future;
  - downtime of 0 to 720 hours.
- **The turbine:** it must exist. A turbine that is faulted, stopped, offline or already in maintenance is noted for
  the reviewer.
- **The Policy Service's rules:** asked on the plan as a proposal. Its block blocks. An escalation is noted, as the
  approval that follows is a person's.
- **What cannot be checked blocks:** if a source cannot be asked, submission does not go ahead (SRS 7.7).
- **Not blocking:** a plan without an impact estimate is noted, as its cost and downtime rules escalate.

**A person drafts or an agent drafts; only a person submits (FR-MNT-003).**
- **Drafting:** needs `maintenance.draft` for the case's turbine, its wind farm or the tenant. An agent may draft:
  - for its principal, the person or the tenant's `fault-investigator`;
  - through WindOps MCP's `create_draft_work_order`, the one write tool any agent holds;
  - only on the Maintenance Planner's allowlist, as it reads no external content (MCP-006).
- **Submitting:** needs `maintenance.submit` and a person. The Maintenance Service refuses any identity with an agent
  behind it, or with no person, before asking Authorization, so no change to the permission table could let an agent
  submit.
- **Withdrawing:** a draft may be withdrawn, by a person only.
- **Reading:** needs `investigation.read`. A work order the caller may not read is not found.

**Recorded:**
- **History:** every change goes in the work order's history, which a database trigger keeps unchanged.
- **Audit:** every change is audited. An agent's change is audited as the agent, on its principal's behalf
  (SEC-010).
- **Announced:** a submission is announced as `workorder.submitted` with its proposal version, for Approval and
  Notification. Both go through the transactional outbox.

## Consequences

- **Proved by `make maintenance-check`, with the real services:**
  - **A planner's draft:**
    - its problems are named;
    - fixed, it passes, with the turbine's real status and the Policy Service's real result;
    - it is submitted, published and audited.
  - **An operator** is refused a draft.
  - **Another tenant** cannot find the work order.
  - **The Maintenance Planner agent**, for the `fault-investigator`, drafts through WindOps MCP with its own token.
  - **Every attempt to submit with an agent behind it is refused**, and no agent's allowlist names a tool that
    submits, approves or reserves.
- **Not yet:**
  - **Inventory checks:** that each part fits and is in stock come with M9.2.
  - **The planner's own drafting:** its prompt, and step 12's draft, come with M9.3.
  - **The screens:** M9.4.
  - **Decisions, new versions and completion:** Phase 10.
- **Procedures** are checked for form here. That each cites a current, approved passage comes with M9.3, where the
  planner cites them.

## Amended 2026-11-20, for M9.5

**The Maintenance Service records its own refusals.** When it refuses an identity with an agent behind it, or with
no person, it has not asked Authorization, so Authorization recorded nothing. It now audits the refusal itself, in
the same shape: `authorization.denied`, naming the permission refused (`maintenance.submit` to submit,
`maintenance.draft` to withdraw), with the agent as the actor on its principal's behalf (SEC-010). The administrator
console's Denials page shows these alongside Authorization's own (SRS 9.4). `make agent-denial-check` proves it, for
the Phase 9 exit gate.
