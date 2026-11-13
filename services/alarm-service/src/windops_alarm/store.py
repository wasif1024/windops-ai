"""The tables this service owns, all tenant-owned with row-level security, and its events."""

import hashlib
from datetime import datetime
from typing import Any

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    LargeBinary,
    MetaData,
    Table,
    Text,
    insert,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "alarm"
CHANGED_SUBJECT = "alarm.changed"
ACTIVE, ACKNOWLEDGED, CLEARED, SHELVED = "active", "acknowledged", "cleared", "shelved"
STATES = (ACTIVE, ACKNOWLEDGED, CLEARED, SHELVED)
METADATA = MetaData()

# One row an occurrence: raised by the device, then cleared by it. The same condition again is a
# new alarm, so each one's history stays its own.
ALARMS = Table(
    "alarms",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("alarm_id", Text, primary_key=True),
    Column("turbine_id", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("tag", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("severity_source", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("raised_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("acknowledged_at", DateTime(timezone=True)),
    Column("acknowledged_by", Text, nullable=False, server_default=""),
    Column("cleared_at", DateTime(timezone=True)),
    Column("shelved_until", DateTime(timezone=True)),
    Column("shelved_by", Text, nullable=False, server_default=""),
    Column("shelve_reason", Text, nullable=False, server_default=""),
    # The state a shelved alarm goes back to.
    Column("shelved_from", Text, nullable=False, server_default=""),
    Column("repeat_count", BigInteger, nullable=False, server_default="0"),
    Column("version", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # At most one standing alarm for a condition on a turbine.
    Index(
        "alarms_one_standing",
        "tenant_id", "turbine_id", "code", "tag",
        unique=True,
        postgresql_where=text("state <> 'cleared'"),
        sqlite_where=text("state <> 'cleared'"),
    ),
    Index("alarms_by_turbine", "tenant_id", "turbine_id", "raised_at"),
)  # fmt: skip
# Every change an alarm went through, by whom (FR-ALM-002).
ALARM_TRANSITIONS = Table(
    "alarm_transitions",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("alarm_id", Text, primary_key=True),
    # The alarm's version after the change.
    Column("version", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("from_state", Text, nullable=False),
    Column("to_state", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("note", Text, nullable=False),
)
# Each report a device sent, so a repeat changes nothing (EVT-003), and a clear that arrives
# before its raise waits for it.
ALARM_REPORTS = Table(
    "alarm_reports",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("turbine_id", Text, primary_key=True),
    Column("report_id", Text, primary_key=True),
    Column("event_id", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("tag", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    # False for a clear with nothing standing to clear yet.
    Column("applied", Boolean, nullable=False),
    Column("alarm_id", Text, nullable=False, server_default=""),
)
# A retry with the same key returns the first answer, and changes nothing (GRP-007).
IDEMPOTENCY_KEYS = Table(
    "idempotency_keys",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("user_id", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("method", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("response", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = ("alarms", "alarm_transitions", "alarm_reports", "idempotency_keys")


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def _hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def remembered(
    connection: AsyncConnection, tenant_id: str, user_id: str, key: str, method: str,
    request: Message,
) -> bytes | None:  # fmt: skip
    """Return the first answer to a retried call, or None if this key is new."""
    if not key:
        return None
    row = (
        await connection.execute(
            select(IDEMPOTENCY_KEYS).where(
                IDEMPOTENCY_KEYS.c.tenant_id == tenant_id,
                IDEMPOTENCY_KEYS.c.user_id == user_id,
                IDEMPOTENCY_KEYS.c.idempotency_key == key,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    if row.method != method or row.request_hash != _hash(request):
        raise KeyReusedError("This idempotency key was used for a different request")
    response: bytes = row.response
    return response


async def remember(
    connection: AsyncConnection, *, tenant_id: str, user_id: str, key: str, method: str,
    request: Message, response: Message, now: datetime,
) -> None:  # fmt: skip
    """Keep the answer in the change's own transaction, so a retry finds one or neither."""
    if key:
        await connection.execute(
            insert(IDEMPOTENCY_KEYS).values(
                tenant_id=tenant_id, user_id=user_id, idempotency_key=key, method=method,
                request_hash=_hash(request), response=response.SerializeToString(),
                created_at=now,
            )
        )  # fmt: skip


_EVENT_STATES = {
    ACTIVE: events_pb2.ALARM_STATE_ACTIVE,
    ACKNOWLEDGED: events_pb2.ALARM_STATE_ACKNOWLEDGED,
    CLEARED: events_pb2.ALARM_STATE_CLEARED,
    SHELVED: events_pb2.ALARM_STATE_SHELVED,
}


async def record_transition(
    connection: AsyncConnection, alarm: Any, *, from_state: str, at: datetime, actor_type: str,
    actor_id: str, note: str = "", audit_action: str = "",
) -> None:  # fmt: skip
    """Keep the change in the alarm's history, and queue its alarm.changed, with it (EVT-002).

    An operator's action is also audited. A device's raise or clear is in the alarm's own history,
    which is the record of what the device said.
    """
    await connection.execute(
        insert(ALARM_TRANSITIONS).values(
            tenant_id=alarm.tenant_id, alarm_id=alarm.alarm_id, version=alarm.version, at=at,
            from_state=from_state, to_state=alarm.state, actor_type=actor_type,
            actor_id=actor_id, note=note,
        )
    )  # fmt: skip
    await add_event(
        connection,
        CHANGED_SUBJECT,
        tenant_id=alarm.tenant_id,
        payload=events_pb2.AlarmChanged(
            alarm_id=alarm.alarm_id, turbine_id=alarm.turbine_id,
            state=_EVENT_STATES[alarm.state],
            code=alarm.code, severity=alarm.severity, version=alarm.version,
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip
    if audit_action:
        await add_event(
            connection,
            audit_request_subject(SERVICE_NAME),
            tenant_id=alarm.tenant_id,
            payload=events_pb2.AuditRequested(
                action=audit_action, actor_type=actor_type, actor_id=actor_id,
                resource_type="alarm", resource_id=alarm.alarm_id, outcome="succeeded",
            ),
            producer=SERVICE_NAME,
        )  # fmt: skip
