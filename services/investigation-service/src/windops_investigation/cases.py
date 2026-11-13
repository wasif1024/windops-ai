"""How a case lives: opened by a person or by detections, grouped by the case rules, cancelled.

    detection (medium or above, nothing active) ──> open ──> investigating ──> pending_review
    a person ───────────────────────────────────> open        (Agent Runtime, Phase 8)
    any active case ──cancel──> cancelled

A detection whose turbine and component already have an active case joins it, and raises its
severity if it is more severe. Each detection is kept once, by its ID, so fault.detected sent twice
changes nothing (EVT-003).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_migrations import in_tenant

from windops_investigation import case_rules
from windops_investigation.store import (
    ACTIVE,
    CANCELLED,
    CASE_DETECTIONS,
    CASES,
    OPEN,
    record,
)

DETECTION, USER, SERVICE = "detection", "user", "service"


def new_case_id() -> str:
    return f"case_{uuid.uuid4().hex[:20]}"


async def load(connection: AsyncConnection, tenant: str, case_id: str) -> Any | None:
    return (
        await connection.execute(
            select(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
        )
    ).one_or_none()


async def change(
    connection: AsyncConnection, case: Any, *, now: datetime, action: str, actor_type: str,
    actor_id: str, note: str = "", **values: Any,
) -> Any:  # fmt: skip
    await connection.execute(
        update(CASES)
        .where(CASES.c.tenant_id == case.tenant_id, CASES.c.case_id == case.case_id)
        .values(version=case.version + 1, updated_at=now, **values)
    )
    changed = await load(connection, case.tenant_id, case.case_id)
    await record(connection, changed, action=action, at=now, actor_type=actor_type,
                 actor_id=actor_id, note=note)  # fmt: skip
    return changed


async def create(
    connection: AsyncConnection, *, tenant: str, turbine_id: str, group: str, title: str,
    severity: str, origin: str, by: str, actor_type: str, now: datetime,
) -> Any:  # fmt: skip
    case_id = new_case_id()
    await connection.execute(
        CASES.insert().values(
            tenant_id=tenant, case_id=case_id, turbine_id=turbine_id, component_group=group,
            title=title, status=OPEN, severity=severity, origin=origin, created_by=by,
            created_at=now, updated_at=now,
            last_detection_at=now if origin == DETECTION else None,
            case_rules_version=case_rules.VERSION, version=1,
        )
    )  # fmt: skip
    case = await load(connection, tenant, case_id)
    await record(connection, case, action="created", at=now, actor_type=actor_type, actor_id=by)
    return case


async def attach_detection(
    connection: AsyncConnection, tenant: str, detected: events_pb2.FaultDetected, by: str,
    now: datetime,
) -> Any | None:  # fmt: skip
    """Put the detection in its case, opening one if the case rules say so. Returns the case,
    or None for a repeat or a low detection with no active case to join."""
    await in_tenant(connection, tenant)
    d = CASE_DETECTIONS.c
    seen = await connection.execute(
        select(d.detection_id).where(d.tenant_id == tenant, d.detection_id == detected.detection_id)
    )
    if seen.first() is not None:
        return None
    group = case_rules.group_of(detected.rule_id)
    c = CASES.c
    active = (
        await connection.execute(
            select(CASES).where(
                c.tenant_id == tenant, c.turbine_id == detected.turbine_id,
                c.component_group == group, c.origin == DETECTION, c.status.in_(ACTIVE),
            )
        )
    ).one_or_none()  # fmt: skip
    if active is None:
        if not case_rules.opens_a_case(detected.severity):
            return None
        title = f"{group.replace('_', ' ').capitalize()}: {detected.rule_id}"
        case = await create(
            connection, tenant=tenant, turbine_id=detected.turbine_id, group=group, title=title,
            severity=detected.severity, origin=DETECTION, by=by, actor_type=SERVICE, now=now,
        )  # fmt: skip
    else:
        severity = case_rules.highest(active.severity, detected.severity)
        action = "severity_raised" if severity != active.severity else "detection_attached"
        case = await change(
            connection, active, now=now, action=action, actor_type=SERVICE, actor_id=by,
            note=detected.detection_id, severity=severity, last_detection_at=now,
        )  # fmt: skip
    await connection.execute(
        CASE_DETECTIONS.insert().values(
            tenant_id=tenant, detection_id=detected.detection_id, case_id=case.case_id,
            rule_id=detected.rule_id, rule_version=detected.rule_version,
            rule_content_hash=detected.rule_content_hash, severity=detected.severity,
            telemetry_snapshot_id=detected.telemetry_snapshot_id, attached_at=now,
        )
    )  # fmt: skip
    return case


async def cancel(
    connection: AsyncConnection, case: Any, *, by: str, reason: str, now: datetime
) -> Any:
    return await change(
        connection, case, now=now, action="cancelled", actor_type=USER, actor_id=by,
        note=reason, status=CANCELLED, cancel_reason=reason,
    )  # fmt: skip
