"""How an alarm lives: raised and cleared by its device, then acknowledged or shelved by people.

    raise ──> active ──acknowledge──> acknowledged
                │  └──shelve──> shelved ──unshelve, or its time is up──> where it was
                └── clear, from any standing state ──> cleared

A raise while the same condition is standing counts as a repeat, not a new alarm. Reports can
arrive out of order: a clear with nothing standing waits, and the raise it belongs to clears at
once when it comes. Each report is kept by its ID, so a repeat changes nothing (EVT-003).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops_migrations import across_tenants, in_tenant

from windops_alarm import catalog
from windops_alarm.store import (
    ACTIVE,
    ALARM_REPORTS,
    ALARMS,
    CLEARED,
    SHELVED,
    record_transition,
)

DEVICE, USER, SYSTEM = "device", "user", "system"


def new_alarm_id() -> str:
    return f"alm_{uuid.uuid4().hex[:20]}"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def load(connection: AsyncConnection, tenant: str, alarm_id: str) -> Any | None:
    return (
        await connection.execute(
            select(ALARMS).where(ALARMS.c.tenant_id == tenant, ALARMS.c.alarm_id == alarm_id)
        )
    ).one_or_none()


async def change(
    connection: AsyncConnection, alarm: Any, *, at: datetime, now: datetime, actor_type: str,
    actor_id: str, note: str = "", audit_action: str = "", **values: Any,
) -> Any:  # fmt: skip
    """Apply one change to a standing alarm, keep it in its history, and announce it."""
    await connection.execute(
        update(ALARMS)
        .where(ALARMS.c.tenant_id == alarm.tenant_id, ALARMS.c.alarm_id == alarm.alarm_id)
        .values(version=alarm.version + 1, updated_at=now, **values)
    )
    changed = await load(connection, alarm.tenant_id, alarm.alarm_id)
    await record_transition(
        connection, changed, from_state=alarm.state, at=at, actor_type=actor_type,
        actor_id=actor_id, note=note, audit_action=audit_action,
    )  # fmt: skip
    return changed


async def clear(
    connection: AsyncConnection, alarm: Any, at: datetime, now: datetime, actor_id: str
) -> Any:
    return await change(
        connection, alarm, at=at, now=now, actor_type=DEVICE, actor_id=actor_id,
        state=CLEARED, cleared_at=at, shelved_until=None, shelved_by="", shelve_reason="",
        shelved_from="",
    )  # fmt: skip


async def _standing(connection: AsyncConnection, tenant: str, report: Any) -> Any | None:
    return (
        await connection.execute(
            select(ALARMS).where(
                ALARMS.c.tenant_id == tenant, ALARMS.c.turbine_id == report.turbine_id,
                ALARMS.c.code == report.code, ALARMS.c.tag == report.tag,
                ALARMS.c.state != CLEARED,
            )
        )
    ).one_or_none()  # fmt: skip


async def apply_report(
    connection: AsyncConnection, tenant: str, event_id: str, report: events_pb2.AlarmReported,
    now: datetime,
) -> bool:  # fmt: skip
    """Apply one device's report, in the caller's transaction. False for a repeat."""
    await in_tenant(connection, tenant)
    reports = ALARM_REPORTS.c
    seen = await connection.execute(
        select(reports.report_id).where(
            reports.tenant_id == tenant, reports.turbine_id == report.turbine_id,
            reports.report_id == report.report_id,
        )
    )  # fmt: skip
    if seen.first() is not None:
        return False
    at = _utc(report.event_time.ToDatetime())
    received = _utc(report.received_at.ToDatetime()) if report.HasField("received_at") else now
    standing = await _standing(connection, tenant, report)
    applied, alarm_id = True, ""
    if report.action == "raise" and standing is not None:
        # The condition is still standing: a repeat, not a change of state.
        await connection.execute(
            update(ALARMS)
            .where(ALARMS.c.tenant_id == tenant, ALARMS.c.alarm_id == standing.alarm_id)
            .values(repeat_count=standing.repeat_count + 1, updated_at=now)
        )
        alarm_id = standing.alarm_id
    elif report.action == "raise":
        alarm_id = await _raise(connection, tenant, report, at, received, now)
    elif standing is not None and _utc(standing.raised_at) <= at:
        await clear(connection, standing, at, now, report.device_id)
        alarm_id = standing.alarm_id
    else:
        # Nothing standing to clear: it waits for the raise it belongs to.
        applied = False
    await connection.execute(
        ALARM_REPORTS.insert().values(
            tenant_id=tenant, turbine_id=report.turbine_id, report_id=report.report_id,
            event_id=event_id, code=report.code, tag=report.tag, action=report.action,
            event_time=at, received_at=received, applied=applied, alarm_id=alarm_id,
        )
    )  # fmt: skip
    return True


async def _raise(
    connection: AsyncConnection, tenant: str, report: events_pb2.AlarmReported, at: datetime,
    received: datetime, now: datetime,
) -> str:  # fmt: skip
    severity, source = catalog.severity_for(report.code, report.severity)
    alarm_id = new_alarm_id()
    await connection.execute(
        ALARMS.insert().values(
            tenant_id=tenant, alarm_id=alarm_id, turbine_id=report.turbine_id,
            code=report.code, tag=report.tag,
            description=catalog.description_for(report.code, report.message),
            severity=severity, severity_source=source, state=ACTIVE,
            source=report.source or report.device_id, raised_at=at, received_at=received,
            repeat_count=0, version=1, updated_at=now,
        )
    )  # fmt: skip
    alarm = await load(connection, tenant, alarm_id)
    await record_transition(
        connection, alarm, from_state="", at=at, actor_type=DEVICE, actor_id=report.device_id
    )
    # A clear that came first, for this condition and no earlier than this raise, ends it now.
    reports = ALARM_REPORTS.c
    waiting = (
        await connection.execute(
            select(ALARM_REPORTS)
            .where(
                reports.tenant_id == tenant, reports.turbine_id == report.turbine_id,
                reports.code == report.code, reports.tag == report.tag,
                reports.action == "clear", reports.applied.is_(False), reports.event_time >= at,
            )
            .order_by(reports.event_time)
            .limit(1)
        )
    ).one_or_none()  # fmt: skip
    if waiting is not None:
        await clear(connection, alarm, _utc(waiting.event_time), now, report.device_id)
        await connection.execute(
            update(ALARM_REPORTS)
            .where(
                reports.tenant_id == tenant, reports.turbine_id == waiting.turbine_id,
                reports.report_id == waiting.report_id,
            )
            .values(applied=True, alarm_id=alarm_id)
        )  # fmt: skip
    return alarm_id


async def expire_shelves(engine: AsyncEngine, now: datetime | None = None) -> int:
    """Put back each shelved alarm whose time is up, as the system. Returns how many."""
    now = now or datetime.now(UTC)
    async with engine.begin() as connection:
        # Platform-wide: every tenant's shelves expire on time.
        await across_tenants(connection)
        due = (
            await connection.execute(
                select(ALARMS).where(ALARMS.c.state == SHELVED, ALARMS.c.shelved_until <= now)
            )
        ).all()
        for alarm in due:
            await in_tenant(connection, alarm.tenant_id)
            await change(
                connection, alarm, at=now, now=now, actor_type=SYSTEM, actor_id=SYSTEM,
                note="Shelved time is up", state=alarm.shelved_from or ACTIVE,
                shelved_until=None, shelved_by="", shelve_reason="", shelved_from="",
            )  # fmt: skip
    return len(due)
