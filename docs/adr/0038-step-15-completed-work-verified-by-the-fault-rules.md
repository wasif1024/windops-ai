# ADR 0038: Step 15, completed work verified by the fault rules

**Status:** Accepted, 2026-11-25, for M10.6.

## Context

The requirements:
- **FR-MNT-004:** record completion data for post-maintenance verification. The Maintenance Service owns it.
- **FR-MNT-005:** compare telemetry after completion with the original condition, and report the outcome to a
  human. The Investigation Service owns it.
- **SRS 5.4 step 15:** start post-maintenance verification when authorized completion data arrives.
- **SRS 4.12:** an approved work order moves to `completed`, then `verified`.

Already in place:
- **`workorder.completed`** was in the contracts, with only a work order ID and a time, and nothing sent it.
- **The original condition:** each case's evidence snapshot holds a frozen telemetry window (EVD-003).
- **Fault Detection's `EvaluateTurbine`** runs every enabled rule on a turbine at a given moment and records nothing.
  Investigation already reads telemetry and alarms as itself, which is what it needs.

The user chose:
- **The case:** a person closes it, or reopens it. Nothing is closed on the outcome alone.
- **The window:** watch the turbine for an hour after completion, as a setting.

## Decision

**Completion, in Maintenance** (FR-MNT-004):
- **`CompleteWorkOrder`** records what was done on an approved work order:
  - when the work started and ended, a summary, the parts used and the actual downtime;
  - who recorded it and when, set by the service.
- **Who:** a person with `maintenance.complete` for the turbine (Maintenance Planner or Manager), never an agent
  (SEC-009). The refusal of an agent is made here, before Authorization is asked, and audited.
- **When:**
  - the work starts no earlier than the approval;
  - it ends no later than now, give or take five minutes for a device's clock.
- **The record:**
  - it is kept on the work order, which becomes `completed`, with the change in its history;
  - it is idempotent by key (GRP-007).
- **`workorder.completed`** now names the case, turbine, version, evidence snapshot, who completed it and when.
  Only identifiers and times are sent: the summary stays with the work order.

**Verification, in Investigation** (FR-MNT-005), on a new durable consumer, `investigation-completions`:
- **Hearing:** each completion is kept once, by work order and version.
  - If the case is `approved` on the snapshot the work stood on, it becomes `verifying`, a new active status.
  - Otherwise, the completion is kept as `not_started`, saying why, and the case does not move.
- **After the window:** a verifier, acting as the service, does three things.
  - It freezes the telemetry from completion to the end of the window as a telemetry snapshot, with its own hash.
  - It summarizes that snapshot and the evidence's telemetry, metric by metric: mean, maximum and readings, before
    and after.
  - It runs the fault rules again with `EvaluateTurbine` at the window's end.
- **The outcome is worked out, never judged by a model:**
  - **inconclusive:** fewer than 10 samples after the work, or a rule that counts finds a data gap;
  - **not resolved:** a rule that counts finds anything else;
  - **resolved:** none does.
- **Which rules count:** the case's own, from its detections, or every rule for a case a person opened.
- **Why the window's end is enough:** each default rule judges a recent part of 10 minutes or less, which fits inside
  the window. Its longer baseline may reach back before the work, but cannot fire on its own.
- **Afterwards:** the case is `verified`, another new active status, and a detection on the same component still
  joins it. The change is announced on `case.updated` as `verification_reported`, which now carries the work order and
  the outcome.
- **When an owner is down:** the verifier tries again, sooner then later, up to every five minutes (SRS 7.7). If it
  still cannot verify a day after the window ends, it reports inconclusive and says why.
- **The setting:** `WINDOPS_VERIFICATION_WINDOW_MINUTES` sets the window, from 1 to 1,440 minutes, default 60. The local
  cluster uses 2 minutes, so its check is quick.

**Reported to a person:**
- **The Notification Service** has a new kind, `case_verified`. By default it goes to the person who opened the case,
  or for a case from detections to those holding `finding.review` there. It names the case, the work order and the
  outcome, and never the completion's summary.
- **`GetCase`** lists each verification with its comparison, the rules' results and both telemetry snapshots.
- **`ReviewVerification`** answers the newest one:
  - **close:** the case is `closed`;
  - **reopen:** the case is `open` again, to be investigated, and needs a reason;
  - **the rules:** it needs `finding.review` and a person, never an agent; it applies only to a `verified` case, at
    the version the person saw; it is idempotent by key.

**The work order follows:** Maintenance hears `verification_reported` on `case.updated`, and the completed work order
becomes `verified`, with the outcome.

## Consequences

- **The check:** `make verification-check` proves it with the real services, in about four minutes:
  - the planner's agent and an operator may not record the work done;
  - the planner does, and the case is verifying;
  - after the window, the telemetry is frozen and compared, and the outcome matches the rules asked again by the check;
  - the work order is verified, and the engineer who opened the case is told;
  - the planner may not answer the report, and the engineer closes the case.
- **The order matters:** a case is `verified` before a person closes it. Approval and Maintenance count `verifying`
  and `verified` as active.
- **A not resolved outcome** waits for a person like any other. Reopening sends the case back to `open`, and a new
  investigation starts from new evidence.
- **The screens:** the completion form and the verification view come with the investigation screens in M11.
- **Revisit if:** a rule's recent part grows past the window, or a tenant wants verification to also compare
  production or availability.
