"""The alarm console's API: standing alarms, one alarm's history, and what operators do (M5.6).

It decides nothing. The Alarm Service shows only the alarms the caller may read, decides who may
acknowledge or shelve one, and never lets anyone change an alarm's severity (FR-ALM-004). Each
call is made as the signed-in user.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from windops.alarm.v1 import alarm_pb2

from windops_web_bff.common import ID_PATTERN, Call, Key, time_of
from windops_web_bff.tokens import Caller

AlarmId = Annotated[str, Path(pattern=ID_PATTERN)]
STATES = {
    "active": alarm_pb2.ALARM_STATE_ACTIVE,
    "acknowledged": alarm_pb2.ALARM_STATE_ACKNOWLEDGED,
    "cleared": alarm_pb2.ALARM_STATE_CLEARED,
    "shelved": alarm_pb2.ALARM_STATE_SHELVED,
}
STATE_NAMES = {number: name for name, number in STATES.items()}
SEVERITIES = {
    alarm_pb2.ALARM_SEVERITY_LOW: "low",
    alarm_pb2.ALARM_SEVERITY_MEDIUM: "medium",
    alarm_pb2.ALARM_SEVERITY_HIGH: "high",
    alarm_pb2.ALARM_SEVERITY_CRITICAL: "critical",
}


class Alarm(BaseModel):
    alarm_id: str
    turbine_id: str
    code: str
    tag: str
    description: str
    # "low", "medium", "high" or "critical", from the device or the versioned catalogue.
    severity: str
    # "device", or "catalog:<version>".
    severity_source: str
    # "active", "acknowledged", "cleared" or "shelved".
    state: str
    source: str
    raised_at: str | None
    received_at: str | None
    acknowledged_at: str | None
    acknowledged_by: str
    cleared_at: str | None
    shelved_until: str | None
    shelved_by: str
    shelve_reason: str
    repeat_count: int
    version: int


class Transition(BaseModel):
    at: str | None
    # "" for the raise, which has no state before it.
    from_state: str
    to_state: str
    # "device", "user" or "system".
    actor_type: str
    actor_id: str
    note: str


class AlarmList(BaseModel):
    alarms: list[Alarm]
    next_page_token: str


class AlarmDetail(BaseModel):
    alarm: Alarm
    transitions: list[Transition]


class CatalogEntry(BaseModel):
    code: str
    description: str
    default_severity: str
    component: str


class Catalog(BaseModel):
    version: str
    entries: list[CatalogEntry]


class Acknowledge(BaseModel):
    comment: str = Field("", max_length=500)


class Shelve(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    # At most a day, so nothing is set aside and forgotten.
    minutes: int = Field(ge=1, le=1440)


def _alarm(message: Any) -> Alarm:
    return Alarm(
        alarm_id=message.alarm_id, turbine_id=message.turbine_id, code=message.code,
        tag=message.tag, description=message.description,
        severity=SEVERITIES.get(message.severity, "unknown"),
        severity_source=message.severity_source, state=STATE_NAMES.get(message.state, "unknown"),
        source=message.source, raised_at=time_of(message, "raised_at"),
        received_at=time_of(message, "received_at"),
        acknowledged_at=time_of(message, "acknowledged_at"),
        acknowledged_by=message.acknowledged_by, cleared_at=time_of(message, "cleared_at"),
        shelved_until=time_of(message, "shelved_until"), shelved_by=message.shelved_by,
        shelve_reason=message.shelve_reason, repeat_count=message.repeat_count,
        version=message.version,
    )  # fmt: skip


def add_alarm_routes(
    router: APIRouter, caller: Callable[..., Awaitable[Caller]], call_as: Call
) -> None:
    @router.get(
        "/alarms",
        response_model=AlarmList,
        summary="Alarms the user may read: standing ones by default, most severe and newest first",
    )
    async def list_alarms(
        who: Annotated[Caller, Depends(caller)],
        state: Annotated[list[str] | None, Query(max_length=4)] = None,
        turbine_id: Annotated[str, Query(max_length=64)] = "",
        wind_farm_id: Annotated[str, Query(max_length=64)] = "",
        page_size: Annotated[int, Query(ge=1, le=500)] = 100,
        page_token: Annotated[str, Query(max_length=64)] = "",
    ) -> AlarmList:
        states = [STATES[name] for name in state or [] if name in STATES]
        answer = await call_as(
            "alarm", "ListAlarms",
            alarm_pb2.ListAlarmsRequest(
                states=states, turbine_id=turbine_id, wind_farm_id=wind_farm_id,
                page_size=page_size, page_token=page_token,
            ),
            who,
        )  # fmt: skip
        return AlarmList(
            alarms=[_alarm(a) for a in answer.alarms], next_page_token=answer.next_page_token
        )

    @router.get(
        "/alarms/catalog",
        response_model=Catalog,
        summary="What each alarm code means, and its default severity, in the version in force",
    )
    async def catalog(who: Annotated[Caller, Depends(caller)]) -> Catalog:
        answer = await call_as("alarm", "GetAlarmCatalog", alarm_pb2.GetAlarmCatalogRequest(), who)
        return Catalog(
            version=answer.version,
            entries=[
                CatalogEntry(code=e.code, description=e.description,
                             default_severity=SEVERITIES.get(e.default_severity, "unknown"),
                             component=e.component)
                for e in answer.entries
            ],
        )  # fmt: skip

    @router.get(
        "/alarms/{alarm_id}", response_model=AlarmDetail, summary="One alarm, with every change"
    )
    async def get_alarm(alarm_id: AlarmId, who: Annotated[Caller, Depends(caller)]) -> AlarmDetail:
        answer = await call_as("alarm", "GetAlarm", alarm_pb2.GetAlarmRequest(alarm_id=alarm_id),
                               who)  # fmt: skip
        return AlarmDetail(
            alarm=_alarm(answer.alarm),
            transitions=[
                Transition(
                    at=time_of(t, "at"), from_state=STATE_NAMES.get(t.from_state, ""),
                    to_state=STATE_NAMES.get(t.to_state, "unknown"), actor_type=t.actor_type,
                    actor_id=t.actor_id, note=t.note,
                )
                for t in answer.transitions
            ],
        )  # fmt: skip

    @router.post(
        "/alarms/{alarm_id}/acknowledge", response_model=Alarm,
        summary="Acknowledge an active alarm",
    )  # fmt: skip
    async def acknowledge(
        alarm_id: AlarmId, body: Acknowledge, key: Key, who: Annotated[Caller, Depends(caller)]
    ) -> Alarm:
        request = alarm_pb2.AcknowledgeAlarmRequest(
            alarm_id=alarm_id, comment=body.comment, idempotency_key=key
        )
        return _alarm((await call_as("alarm", "AcknowledgeAlarm", request, who)).alarm)

    @router.post(
        "/alarms/{alarm_id}/shelve", response_model=Alarm,
        summary="Set a standing alarm aside for a while, with a reason",
    )  # fmt: skip
    async def shelve(
        alarm_id: AlarmId, body: Shelve, key: Key, who: Annotated[Caller, Depends(caller)]
    ) -> Alarm:
        request = alarm_pb2.ShelveAlarmRequest(
            alarm_id=alarm_id, reason=body.reason, minutes=body.minutes, idempotency_key=key
        )
        return _alarm((await call_as("alarm", "ShelveAlarm", request, who)).alarm)

    @router.post(
        "/alarms/{alarm_id}/unshelve", response_model=Alarm,
        summary="Put a shelved alarm back before its time is up",
    )  # fmt: skip
    async def unshelve(
        alarm_id: AlarmId, key: Key, who: Annotated[Caller, Depends(caller)]
    ) -> Alarm:
        request = alarm_pb2.UnshelveAlarmRequest(alarm_id=alarm_id, idempotency_key=key)
        return _alarm((await call_as("alarm", "UnshelveAlarm", request, who)).alarm)
