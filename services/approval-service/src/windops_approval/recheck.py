"""Whether a decision may go ahead on what is current now (EVD-009, FR-APR-003).

Pure, so each rule is tested on its own. The caller reads the work order and the case as the
decider; this only compares.
"""

from dataclasses import dataclass
from typing import Any

SUBMITTED, STALE = "submitted", "stale"
ACTIVE_CASES = ("open", "investigating", "pending_review", "approved", "verifying", "verified")
# What a submitted work order's policy result may be. A blocked one could not be submitted.
DECIDABLE_POLICY = ("pass", "escalate")


@dataclass(frozen=True)
class Refusal:
    reason: str
    # The request can never be decided again: its proposal or evidence has moved on.
    supersedes: bool


def refusal(row: Any, work_order: Any, case: Any) -> Refusal | None:
    """None when the request still stands for the current proposal and evidence."""
    if work_order.version != row.proposal_version:
        return Refusal(f"The work order is now at version {work_order.version}, not "
                       f"{row.proposal_version}", supersedes=True)  # fmt: skip
    if work_order.status == STALE:
        # EVD-007: a stale recommendation is never approved, and says which rule fired.
        return Refusal(f"The work order is stale: {work_order.stale_rule} "
                       f"({work_order.stale_detail})", supersedes=True)  # fmt: skip
    if work_order.status != SUBMITTED:
        return Refusal(f"The work order is now {work_order.status}", supersedes=True)
    if work_order.evidence_snapshot_id != row.evidence_snapshot_id:
        return Refusal("The work order now refers to snapshot "
                       f"{work_order.evidence_snapshot_id}", supersedes=True)  # fmt: skip
    if case.stale_rule:
        return Refusal(f"The case's evidence is stale: {case.stale_rule} ({case.stale_detail})",
                       supersedes=True)  # fmt: skip
    if case.status not in ACTIVE_CASES:
        return Refusal(f"The case is now {case.status}", supersedes=True)
    if case.evidence_snapshot_id != row.evidence_snapshot_id:
        return Refusal(f"The case's evidence was superseded by snapshot "
                       f"{case.evidence_snapshot_id or 'none'}", supersedes=True)  # fmt: skip
    if work_order.validation.policy_outcome not in DECIDABLE_POLICY:
        # Not superseded: the policy result may be read again once the Policy Service answers.
        return Refusal("The work order has no policy result that allows a decision",
                       supersedes=False)  # fmt: skip
    return None


# The separation-of-duties rules below, by version, so each decision names the rules it was held
# to (RBAC-005). A change to them is a new version.
APPROVAL_RULES = "separation-of-duties-2"
# The work order's history actions that make their actor a requester (RBAC-004): version 2 adds
# revising a stale or rejected work order as a new version (M10.3). Validating changes nothing of
# the plan, so it does not.
REQUESTING_ACTIONS = ("drafted", "updated", "revised", "submitted")


def requesters(row: Any, history: Any) -> list[str]:
    """Who may not decide it (RBAC-004, RBAC-008): whoever drafted, submitted or changed it.

    An agent acts for its principal, recorded as the actor, so a draft an agent prepared names
    the person who started the investigation, or the fault-investigator for a fault-triggered
    one, which is a service and never eligible anyway.
    """
    found = [row.drafted_by, row.submitted_by]
    found += [event.actor_id for event in history if event.action in REQUESTING_ACTIONS]
    return sorted({who for who in found if who})
