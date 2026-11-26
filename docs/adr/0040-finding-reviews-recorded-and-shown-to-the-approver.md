# ADR 0040: Finding reviews, recorded and shown to the approver

**Status:** Accepted, 2026-11-26, for M11.1.

## Context

- **FR-INV-006:** let an authorized user review, challenge, annotate, accept or reject a finding.
- **SRS 4.11:** the console shows the investigation list, timeline, live progress and findings.
- **The findings** are the investigation run's report (SRS 5.7): observed facts, hypotheses, conflicting evidence,
  data gaps, risk flags and recommended actions, each with its citations. The Agent Runtime keeps it, and only the
  Investigation Service may call the Agent Runtime (ADR 0016).
- **No finding has an ID.** A report never changes once kept, though.
- **The user chose:** a challenge or rejection is recorded and shown to whoever decides on the case's work order. It
  blocks nothing.

## Decision

**The Investigation Service serves the report and keeps the reviews:**
- **`GetInvestigationReport`:** the report of the case's investigation run, read from the Agent Runtime as the
  service once the caller may read the case (`investigation.read`), with every review.
- **Naming a finding:** by where it is in its run's report, such as `hypotheses[0]`. The report never changes, so the
  name always points at the same finding.
- **`ReviewFinding`:**
  - **Who:** a person with `finding.review` (Operations Managers and Reliability Engineers), never an agent.
  - **What:** one finding is accepted, rejected, challenged or annotated. Anything but accepting needs a note.
  - **Retries:** a retry with the same key answers the same (GRP-007).
- **What a review is:**
  - It is kept as made in `case_finding_reviews`, and a database trigger refuses any change. A later review is
    another row.
  - It is in the case's history as `finding_accepted`, `finding_rejected`, `finding_challenged` or
    `finding_annotated`, which also audits and announces it.

**What a review does:** nothing beyond being seen.
- **The approval page** shows the findings whose latest review is a challenge or a rejection, with each reviewer's
  note, beside the work order.
- **An annotation** never closes a concern, and an acceptance does.
- **The Approval Service** is unchanged, so the Maintenance Manager decides knowing the concerns.

**The screens (M11.1):**
- **The list:** the active cases, or all of them, and a form to investigate a turbine.
- **A case:**
  - its live progress, read with `fetch` from Web BFF's server-sent events, resumed with `Last-Event-ID`;
  - its findings with their reviews;
  - its evidence and timeline;
  - its verification: the before-and-after comparison, the rules' results, and closing or reopening (ADR 0038).
- **The planner's work order:** a form to record approved work done (FR-MNT-004), and what was recorded.

## Consequences

- **The check:** `make frontend-check` has a Reliability Engineer, on a desktop and on a phone:
  - investigate the demo turbine from the screen;
  - follow the rehearsed run live to review;
  - challenge a finding, which is kept beside it.
- **The findings are labelled as the agents' own:** hypotheses and cited facts, to be checked against their evidence
  (SRS 5.8).
- **Revisit if:** a tenant wants an open challenge to hold the approval. That would be a rule in the Approval
  Service, not here.
