"""What each notification says, and whom it goes to by default (M10.5, ADR 0037).

**Kinds**, each from one event:

| Kind | From |
| --- | --- |
| `approval_requested` | `workorder.submitted` |
| `decision_made` | `approval.decided` |
| `case_pending_review` | `case.updated`, its investigation completed (SRS 5.4 step 13) |
| `case_approved`, `case_rejected` | `case.updated`, a decision resumed it (step 14) |
| `case_needs_attention` | `case.updated`: its run ended otherwise (LLM-010), or a resume stopped |
| `case_verified` | `case.updated`: the work was verified, with its outcome (step 15, M10.6) |

**Audiences:** `requester` is the person who opened the case; a case from detections has none, so
it is everyone who may review findings on that turbine. `drafter` and `submitter` are the work
order's, people only. `holders:<permission>` is everyone holding it where it happened.

**What it says** (FR-NOT-002): identifiers and a link. Never evidence, a decision's reasons,
anyone's name or address, or a secret.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from windops_notification.store import CHANNELS, EMAIL, IN_APP

APPROVAL_REQUESTED, DECISION_MADE = "approval_requested", "decision_made"
CASE_PENDING_REVIEW, CASE_APPROVED = "case_pending_review", "case_approved"
CASE_REJECTED, CASE_NEEDS_ATTENTION = "case_rejected", "case_needs_attention"
CASE_VERIFIED = "case_verified"
KINDS = (
    APPROVAL_REQUESTED, DECISION_MADE, CASE_PENDING_REVIEW, CASE_APPROVED, CASE_REJECTED,
    CASE_NEEDS_ATTENTION, CASE_VERIFIED,
)  # fmt: skip
# case.updated's actions that tell someone something, and the kind each makes.
CASE_ACTIONS = {
    "investigation_completed": CASE_PENDING_REVIEW,
    "approved": CASE_APPROVED,
    "rejected": CASE_REJECTED,
    "investigation_ended": CASE_NEEDS_ATTENTION,
    "decision_not_resumed": CASE_NEEDS_ATTENTION,
    "verification_reported": CASE_VERIFIED,
}
# A verification's outcome, as a notification says it.
OUTCOMES = {
    "resolved": "the fault rules no longer find it",
    "not_resolved": "the fault rules still find it",
    "inconclusive": "there was too little data to say",
}
REQUESTER, DRAFTER, SUBMITTER, HOLDERS = "requester", "drafter", "submitter", "holders:"
# Who may review a case's findings: a detection case's requester (SRS 3.2).
REVIEWERS = "finding.review"
_PERMISSION = re.compile(r"[a-z_]{1,40}\.[a-z_]{1,40}")


@dataclass(frozen=True)
class Rule:
    kind: str
    audience: str
    channels: tuple[str, ...]


BOTH = (IN_APP, EMAIL)
DEFAULTS = (
    # A work order waits for someone who may approve it (FR-APR-001): never its drafter or
    # submitter, who may not (RBAC-004).
    Rule(APPROVAL_REQUESTED, f"{HOLDERS}maintenance.approve", BOTH),
    Rule(DECISION_MADE, DRAFTER, BOTH),
    Rule(DECISION_MADE, SUBMITTER, BOTH),
    Rule(CASE_PENDING_REVIEW, REQUESTER, BOTH),
    Rule(CASE_APPROVED, REQUESTER, BOTH),
    Rule(CASE_REJECTED, REQUESTER, BOTH),
    Rule(CASE_NEEDS_ATTENTION, REQUESTER, BOTH),
    # The outcome is reported to a person, who closes or reopens the case (FR-MNT-005).
    Rule(CASE_VERIFIED, REQUESTER, BOTH),
)


class InvalidRuleError(ValueError):
    pass


def check(kind: str, audience: str, channels: Iterable[str]) -> Rule:
    """A rule as given, or InvalidRuleError saying what is wrong with it."""
    if kind not in KINDS:
        raise InvalidRuleError(f"kind must be one of: {', '.join(KINDS)}")
    if audience.startswith(HOLDERS):
        if not _PERMISSION.fullmatch(audience.removeprefix(HOLDERS)):
            raise InvalidRuleError("holders: must name a permission, such as maintenance.approve")
    elif audience not in (REQUESTER, DRAFTER, SUBMITTER):
        raise InvalidRuleError("audience must be requester, drafter, submitter or holders:<p>")
    if audience in (DRAFTER, SUBMITTER) and kind not in (APPROVAL_REQUESTED, DECISION_MADE):
        raise InvalidRuleError("Only a work order's notifications have a drafter and submitter")
    chosen = tuple(sorted(set(channels)))
    if not chosen or any(c not in CHANNELS for c in chosen):
        raise InvalidRuleError("channels must be in_app, email, or both")
    return Rule(kind, audience, chosen)


def is_person(user: str) -> bool:
    """A user ID, not a service or a principal for fault-triggered work, as "service:..." is."""
    return bool(user) and ":" not in user


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    link: str


def message(kind: str, facts: Mapping[str, str]) -> Message:
    """What a notification says: identifiers and a link, from the event's facts only."""
    case, turbine = facts.get("case_id", ""), facts.get("turbine_id", "")
    order, version = facts.get("work_order_id", ""), facts.get("proposal_version", "")
    on = f"case {case} on turbine {turbine}" if turbine else f"case {case}"
    if kind == APPROVAL_REQUESTED:
        return Message(f"Work order {order} v{version} awaits approval",
                       f"Submitted for {on}. Review it in the approval queue.",
                       "/screens/approvals")  # fmt: skip
    if kind == DECISION_MADE:
        decided = facts.get("decision", "")
        return Message(f"Work order {order} v{version} was {decided}",
                       f"A Maintenance Manager decided on it, for {on}.",
                       "/screens/maintenance")  # fmt: skip
    if kind == CASE_VERIFIED:
        outcome = facts.get("outcome", "")
        said = OUTCOMES.get(outcome, "see the case")
        return Message(f"Case {case}: work verified, {outcome.replace('_', ' ') or 'reported'}",
                       f"After work order {order}, {said}. Close the case or reopen it, for "
                       f"{on}.", "/screens/investigations")  # fmt: skip
    text = {
        CASE_PENDING_REVIEW: ("is ready for review", "Its investigation completed."),
        CASE_APPROVED: ("was approved", "Its work order was approved, and waits for the work."),
        CASE_REJECTED: ("needs a revised proposal", "Its work order was rejected."),
        CASE_NEEDS_ATTENTION: ("needs attention", "Its workflow stopped before a result: "
                               "open the case to see why, and restart it from its evidence."),
    }  # fmt: skip
    headline, more = text[kind]
    return Message(f"Case {case} {headline}", f"{more} {on[0].upper()}{on[1:]}.",
                   "/screens/investigations")  # fmt: skip
