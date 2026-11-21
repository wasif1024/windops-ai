"""Whether a decision may go ahead on what is current now (EVD-009, FR-APR-003).

Pure, so each rule is tested on its own. The caller reads the work order and the case as the
decider; this only compares.
"""

from dataclasses import dataclass
from typing import Any

SUBMITTED = "submitted"
ACTIVE_CASES = ("open", "investigating", "pending_review")
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
    if work_order.status != SUBMITTED:
        return Refusal(f"The work order is now {work_order.status}", supersedes=True)
    if work_order.evidence_snapshot_id != row.evidence_snapshot_id:
        return Refusal("The work order now refers to snapshot "
                       f"{work_order.evidence_snapshot_id}", supersedes=True)  # fmt: skip
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
