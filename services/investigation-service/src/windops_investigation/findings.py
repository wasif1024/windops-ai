"""People's reviews of an investigation's findings (FR-INV-006, M11.1).

The findings are the investigation run's report (SRS 5.7), which the Agent Runtime keeps and only
this service may read. A finding is named by where it is in that report, such as `hypotheses[0]`,
and the report never changes once kept, so the name always points at the same finding.

A review accepts, rejects, challenges or annotates one finding, with a note, which a rejection,
challenge or annotation needs. It is kept as made, audited, and recorded in the case's history. It
decides nothing: the Maintenance Manager who decides on the case's work order sees it beside the
proposal, and decides knowing it.
"""

import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from windops_investigation import cases
from windops_investigation.store import FINDING_REVIEWS

ACTIONS = ("accept", "reject", "challenge", "annotate")
# Each review's action in the case's history.
HISTORY = {
    "accept": "finding_accepted", "reject": "finding_rejected",
    "challenge": "finding_challenged", "annotate": "finding_annotated",
}  # fmt: skip
# The parts of a report a person reviews, each a list.
SECTIONS = (
    "observed_facts", "hypotheses", "conflicting_evidence", "data_gaps", "risk_flags",
    "recommended_actions",
)  # fmt: skip
MAXIMUM_NOTE = 1000
_NAME = re.compile(r"(?P<section>[a-z_]+)\[(?P<index>\d{1,4})\]")


class NoSuchFindingError(ValueError):
    pass


def findings_of(report_json: str) -> dict[str, Any]:
    """Every reviewable finding in a report, by name."""
    report = json.loads(report_json) if report_json else {}
    found: dict[str, Any] = {}
    for section in SECTIONS:
        for index, item in enumerate(report.get(section) or []):
            found[f"{section}[{index}]"] = item
    return found


def check(report_json: str, finding: str, action: str, note: str) -> None:
    """Raise ValueError when the review cannot be made as asked."""
    if action not in ACTIONS:
        raise ValueError(f"action is one of: {', '.join(ACTIONS)}")
    if len(note) > MAXIMUM_NOTE or (action != "accept" and not note):
        raise ValueError(f"A note of at most {MAXIMUM_NOTE} characters, needed but to accept")
    if not _NAME.fullmatch(finding) or finding not in findings_of(report_json):
        raise NoSuchFindingError(f"The report has no finding {finding!r}")


def new_review_id() -> str:
    """Sorted as made, even within one instant: the clock's nanoseconds, then a random tail."""
    return f"frv_{time.time_ns():016x}{uuid.uuid4().hex[:4]}"


async def of_case(connection: AsyncConnection, tenant: str, case_id: str) -> list[Any]:
    r = FINDING_REVIEWS.c
    return list((await connection.execute(
        select(FINDING_REVIEWS).where(r.tenant_id == tenant, r.case_id == case_id)
        .order_by(r.reviewed_at, r.review_id)
    )).all())  # fmt: skip


async def record(
    connection: AsyncConnection, case: Any, *, run_id: str, finding: str, action: str, note: str,
    by: str, now: datetime,
) -> Any:  # fmt: skip
    """Keep the review, and record it in the case's history, under the case's row lock."""
    review_id = new_review_id()
    await connection.execute(FINDING_REVIEWS.insert().values(
        tenant_id=case.tenant_id, review_id=review_id, case_id=case.case_id, run_id=run_id,
        finding=finding, action=action, note=note, reviewed_by=by, reviewed_at=now,
    ))  # fmt: skip
    await cases.change(
        connection, case, now=now, action=HISTORY[action],
        actor_type=cases.USER, actor_id=by, note=f"{finding}: {note}"[:1000],
    )  # fmt: skip
    r = FINDING_REVIEWS.c
    return (await connection.execute(select(FINDING_REVIEWS).where(
        r.tenant_id == case.tenant_id, r.review_id == review_id))).one()  # fmt: skip
