"""A work order's plan, checked field by field (FR-MNT-002). Pure: no service is asked here.

The turbine's status and the Policy Service's rules are checked in validation.py, which adds
their problems to these.
"""

import re
from datetime import datetime

from windops.maintenance.v1 import maintenance_pb2 as pb

ACTIONS = ("inspection", "repair", "replacement", "adjustment", "monitoring")
# Work on the component itself, which needs an approved procedure to follow.
NEEDS_PROCEDURE = ("repair", "replacement", "adjustment")
CITATION = re.compile(r"kn:[A-Z0-9][A-Z0-9-]{2,63}@\d{1,4}#\d{1,5}")
PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,63}")
MAXIMUM_TEXT, MAXIMUM_SUMMARY = 200, 4000
MAXIMUM_TASKS, MAXIMUM_PARTS, MAXIMUM_PROCEDURES = 50, 50, 20
MAXIMUM_TASK_HOURS, MAXIMUM_CREW, MAXIMUM_DOWNTIME_HOURS = 240.0, 20, 720.0


def _problem(field: str, code: str, message: str, blocking: bool = True) -> pb.Problem:
    return pb.Problem(field=field, code=code, message=message, blocking=blocking)


def shape(plan: pb.Plan) -> list[pb.Problem]:
    """Problems that make a plan unreadable: refused outright, never kept as a draft."""
    found = []
    if len(plan.title) > MAXIMUM_TEXT or len(plan.summary) > MAXIMUM_SUMMARY:
        found.append(
            _problem(
                "title", "too_long", f"title is at most {MAXIMUM_TEXT}, summary {MAXIMUM_SUMMARY}"
            )
        )
    if len(plan.tasks) > MAXIMUM_TASKS or len(plan.parts) > MAXIMUM_PARTS:
        found.append(_problem("tasks", "too_many", f"at most {MAXIMUM_TASKS} tasks and parts"))
    if len(plan.procedures) > MAXIMUM_PROCEDURES:
        found.append(_problem("procedures", "too_many", f"at most {MAXIMUM_PROCEDURES} procedures"))
    if any(len(t.description) > MAXIMUM_SUMMARY for t in plan.tasks):
        found.append(_problem("tasks", "too_long", "a task is too long"))
    return found


def check(plan: pb.Plan, now: datetime) -> list[pb.Problem]:
    """What a plan needs before it can be submitted. A draft may lack any of it."""
    found = []
    if not plan.title.strip():
        found.append(_problem("title", "missing", "A work order needs a title"))
    if plan.action not in ACTIONS:
        found.append(_problem("action", "invalid", f"action is one of: {', '.join(ACTIONS)}"))
    if not plan.summary.strip():
        found.append(_problem("summary", "missing", "Say what the work is and why"))
    if not plan.tasks:
        found.append(_problem("tasks", "missing", "A plan needs at least one task"))
    for index, task in enumerate(plan.tasks):
        where = f"tasks[{index}]"
        if not task.description.strip():
            found.append(_problem(f"{where}.description", "missing", "Describe the task"))
        if not 0 < task.duration_hours <= MAXIMUM_TASK_HOURS:
            found.append(
                _problem(
                    f"{where}.duration_hours",
                    "out_of_range",
                    f"A task takes more than 0 and at most {MAXIMUM_TASK_HOURS} hours",
                )
            )
        if not 1 <= task.crew_size <= MAXIMUM_CREW:
            found.append(
                _problem(
                    f"{where}.crew_size", "out_of_range", f"A crew is 1 to {MAXIMUM_CREW} people"
                )
            )
    named: set[str] = set()
    for index, part in enumerate(plan.parts):
        if not PART.fullmatch(part.part_number) or not 1 <= part.quantity <= 1000:
            found.append(
                _problem(
                    f"parts[{index}]",
                    "invalid",
                    "A part needs its number and a quantity from 1 to 1000",
                )
            )
        elif part.part_number in named:
            found.append(
                _problem(
                    f"parts[{index}]",
                    "duplicate",
                    f"{part.part_number} is named twice; give it once, with the total",
                )
            )
        named.add(part.part_number)
    bad = [c for c in plan.procedures if not CITATION.fullmatch(c)]
    if bad:
        found.append(
            _problem(
                "procedures",
                "invalid",
                "Cite procedures as knowledge passages, such as kn:DOC@3#12",
            )
        )
    if plan.action in NEEDS_PROCEDURE and not plan.procedures:
        found.append(
            _problem(
                "procedures", "missing", f"A {plan.action} follows an approved procedure: cite one"
            )
        )
    if not plan.HasField("planned_start") or not plan.HasField("planned_end"):
        found.append(_problem("planned_start", "missing", "A plan needs its window"))
    else:
        start, end = plan.planned_start.ToDatetime(), plan.planned_end.ToDatetime()
        if end <= start:
            found.append(_problem("planned_end", "out_of_range", "The window ends after it starts"))
        if start <= now.replace(tzinfo=None):
            found.append(_problem("planned_start", "in_the_past", "The window starts in future"))
    if not 0 <= plan.estimated_downtime_hours <= MAXIMUM_DOWNTIME_HOURS:
        found.append(
            _problem(
                "estimated_downtime_hours",
                "out_of_range",
                f"Downtime is 0 to {MAXIMUM_DOWNTIME_HOURS} hours",
            )
        )
    if not plan.impact_result_id:
        # The Policy Service escalates cost and downtime without an estimate: a reviewer's matter.
        found.append(
            _problem(
                "impact_result_id",
                "missing",
                "No production impact estimate: its rules escalate",
                blocking=False,
            )
        )
    return found


def needs_climb(plan: pb.Plan) -> bool:
    return any(task.requires_climb for task in plan.tasks)
