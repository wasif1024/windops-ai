"""The Alarm Service through a real gRPC server and SQL database, with stand-in Authorization and
Asset services. SQLite has no row-level security, so `make alarm-check` proves that on PostgreSQL.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.alarm.v1 import alarm_pb2, alarm_pb2_grpc
from windops.events.v1 import events_pb2
from windops_alarm import catalog
from windops_alarm.consumer import handle
from windops_alarm.lifecycle import expire_shelves
from windops_alarm.service import AlarmService, Readable
from windops_alarm.store import METADATA, SERVICE_NAME
from windops_events import Event, InvalidEventError, parse_event
from windops_grpc import (
    NotFoundError,
    ServiceRuntime,
    Settings,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(
    environment="test", version="test", identity_secret=SECRET, grpc_port=0, http_port=0,
    shutdown_grace_seconds=1,
)  # fmt: skip
START = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)
FARMS = {"dogger-east-t01": "dogger-east", "dogger-east-t02": "dogger-east",
         "hornsea-west-t01": "hornsea-west"}  # fmt: skip
OIL = ("GBX_OIL_TEMP_HIGH", "gearbox_oil_temp_c")


@dataclass
class FakeAuthorizer:
    """Grants (user, permission, wind farm). An empty wind farm means the whole tenant."""

    grants: set[tuple[str, str, str]] = field(default_factory=set)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        user = current_identity().user_id or ""
        return (user, permission, "") in self.grants or (
            bool(wind_farm_id) and (user, permission, wind_farm_id) in self.grants
        )

    async def readable(self, permission: str) -> Readable:
        user = current_identity().user_id or ""
        farms = {f for who, p, f in self.grants if who == user and p == permission}
        return Readable("" in farms, frozenset(f for f in farms if f), frozenset())


class FakeAssets:
    async def wind_farm_of(self, turbine_id: str) -> str:
        if turbine_id not in FARMS:
            raise NotFoundError("No such turbine")
        return FARMS[turbine_id]

    async def turbines_in(self, wind_farm_id: str) -> list[str]:
        return sorted(t for t, f in FARMS.items() if f == wind_farm_id)


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    clock: list[datetime]

    async def report(self, action: str, *, at: int = 0, code: str = OIL[0], tag: str = OIL[1],
                     turbine: str = "dogger-east-t01", severity: str = "high",
                     tenant: str = TENANT, report_id: str = "") -> bool:  # fmt: skip
        moment = Timestamp()
        moment.FromDatetime(START + timedelta(seconds=at))
        payload = events_pb2.AlarmReported(
            turbine_id=turbine, device_id="dev_1", code=code, tag=tag, action=action,
            report_id=report_id or f"{code}:{tag}:{action}:{at}", event_time=moment,
            received_at=moment, severity=severity, message="From the device",
            source="windops-simulator",
        )  # fmt: skip
        event = Event("alarm.reported", f"evt-{action}-{at}-{code}", tenant, START, "telemetry",
                      payload, "", "")  # fmt: skip
        return await handle(self.engine, event)

    async def standing(self, user: str = "u-operator", **request: Any) -> list[Any]:
        answer = await self.stub.ListAlarms(
            alarm_pb2.ListAlarmsRequest(**request), metadata=as_user(user), timeout=5
        )
        return list(answer.alarms)


def as_user(user: str = "u-operator", tenant: str = TENANT) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=tenant, user_id=user,
                                auth_level=None, auth_time=int(time.time()))  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def changes(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE))).all()
    return [parse_event(row.subject, row.envelope) for row in rows]


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


def with_service(scenario: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        authorizer = FakeAuthorizer(
            {
                ("u-operator", "alarm.read", ""), ("u-operator", "alarm.acknowledge", ""),
                ("u-auditor", "alarm.read", ""),
                ("u-dogger", "alarm.read", "dogger-east"),
                ("u-dogger", "alarm.acknowledge", "dogger-east"),
            }
        )  # fmt: skip
        clock = [START + timedelta(minutes=5)]

        def register(server: grpc.aio.Server) -> None:
            alarm_pb2_grpc.add_AlarmServiceServicer_to_server(
                AlarmService(engine, authorizer, FakeAssets(), clock=lambda: clock[0]), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                await scenario(World(alarm_pb2_grpc.AlarmServiceStub(channel), engine, clock))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_raise_makes_an_active_alarm_a_repeat_counts_and_a_clear_ends_it() -> None:
    """FR-ALM-001, FR-ALM-002, FR-ALM-003."""

    async def scenario(world: World) -> None:
        assert await world.report("raise", at=0)
        assert not await world.report("raise", at=0), "the same report again changes nothing"
        assert await world.report("raise", at=60)
        (alarm,) = await world.standing()
        assert (alarm.code, alarm.tag, alarm.state, alarm.repeat_count) == (
            *OIL, alarm_pb2.ALARM_STATE_ACTIVE, 1,
        )  # fmt: skip
        assert alarm.raised_at.ToDatetime(tzinfo=UTC) == START
        assert await world.report("clear", at=300)
        assert await world.standing() == []
        cleared = await world.standing(states=[alarm_pb2.ALARM_STATE_CLEARED])
        assert cleared[0].cleared_at.ToDatetime(tzinfo=UTC) == START + timedelta(seconds=300)
        history = await world.stub.GetAlarm(
            alarm_pb2.GetAlarmRequest(alarm_id=alarm.alarm_id), metadata=as_user(), timeout=5
        )
        assert [(t.to_state, t.actor_type) for t in history.transitions] == [
            (alarm_pb2.ALARM_STATE_ACTIVE, "device"), (alarm_pb2.ALARM_STATE_CLEARED, "device"),
        ]  # fmt: skip
        announced = [e for e in await changes(world.engine) if e.subject == "alarm.changed"]
        assert [(e.payload.state, e.payload.version, e.payload.severity) for e in announced] == [
            (events_pb2.ALARM_STATE_ACTIVE, 1, "high"), (events_pb2.ALARM_STATE_CLEARED, 2, "high"),
        ]  # fmt: skip
        # The same condition again is a new occurrence, with its own history.
        await world.report("raise", at=900)
        (again,) = await world.standing()
        assert again.alarm_id != alarm.alarm_id

    with_service(scenario)


def test_a_clear_that_arrives_first_waits_for_its_raise() -> None:
    async def scenario(world: World) -> None:
        assert await world.report("clear", at=120)
        assert await world.standing() == []
        await world.report("raise", at=60)
        assert await world.standing() == [], "its clear came first, so it ends at once"
        (alarm,) = await world.standing(states=[alarm_pb2.ALARM_STATE_CLEARED])
        assert alarm.cleared_at.ToDatetime(tzinfo=UTC) == START + timedelta(seconds=120)

    with_service(scenario)


def test_severity_comes_from_the_device_or_the_versioned_catalogue_and_never_changes() -> None:
    """FR-ALM-004."""

    async def scenario(world: World) -> None:
        await world.report("raise", severity="critical")
        await world.report("raise", code="NACELLE_VIBRATION_HIGH", tag="v", severity="")
        await world.report("raise", code="NEW_VENDOR_CODE", tag="", severity="")
        found = {a.code: (a.severity, a.severity_source) for a in await world.standing()}
        from_catalog = (alarm_pb2.ALARM_SEVERITY_MEDIUM, f"catalog:{catalog.VERSION}")
        assert found == {
            "GBX_OIL_TEMP_HIGH": (alarm_pb2.ALARM_SEVERITY_CRITICAL, "device"),
            "NACELLE_VIBRATION_HIGH": from_catalog,
            "NEW_VENDOR_CODE": from_catalog,
        }
        # Nothing an operator does can change it.
        oil = next(a for a in await world.standing() if a.code == OIL[0])
        await world.stub.AcknowledgeAlarm(
            alarm_pb2.AcknowledgeAlarmRequest(alarm_id=oil.alarm_id), metadata=as_user(), timeout=5
        )
        oil_now = next(a for a in await world.standing() if a.code == OIL[0])
        assert oil_now.severity == alarm_pb2.ALARM_SEVERITY_CRITICAL
        catalogue = await world.stub.GetAlarmCatalog(
            alarm_pb2.GetAlarmCatalogRequest(), metadata=as_user(), timeout=5
        )
        assert catalogue.version == catalog.VERSION and len(catalogue.entries) == 7

    with_service(scenario)


def test_an_operator_acknowledges_an_alarm_once_with_a_retry_and_it_is_audited() -> None:
    async def scenario(world: World) -> None:
        await world.report("raise")
        (alarm,) = await world.standing()
        request = alarm_pb2.AcknowledgeAlarmRequest(
            alarm_id=alarm.alarm_id, comment="Crew informed", idempotency_key="ack-1"
        )
        first = await world.stub.AcknowledgeAlarm(request, metadata=as_user(), timeout=5)
        again = await world.stub.AcknowledgeAlarm(request, metadata=as_user(), timeout=5)
        assert first == again
        assert (first.alarm.state, first.alarm.acknowledged_by) == (
            alarm_pb2.ALARM_STATE_ACKNOWLEDGED, "u-operator",
        )  # fmt: skip
        audited = [e.payload for e in await changes(world.engine)
                   if e.subject == "audit.requested.alarm"]  # fmt: skip
        assert [(a.action, a.actor_id, a.resource_id) for a in audited] == [
            ("alarm.acknowledged", "u-operator", alarm.alarm_id),
        ]
        # Acknowledged already, so a new acknowledgement is refused.
        twice = alarm_pb2.AcknowledgeAlarmRequest(alarm_id=alarm.alarm_id)
        assert (
            await code_of(world.stub.AcknowledgeAlarm(twice, metadata=as_user(), timeout=5))
            == grpc.StatusCode.FAILED_PRECONDITION
        )
        # Clearing keeps who acknowledged it.
        await world.report("clear", at=600)
        (cleared,) = await world.standing(states=[alarm_pb2.ALARM_STATE_CLEARED])
        assert cleared.acknowledged_by == "u-operator"

    with_service(scenario)


def test_a_shelved_alarm_comes_back_when_its_time_is_up_or_when_unshelved() -> None:
    async def scenario(world: World) -> None:
        await world.report("raise")
        await world.report("raise", code="MAIN_BEARING_TEMP_HIGH", tag="main_bearing_temp_c")
        oil, bearing = sorted(await world.standing(), key=lambda a: a.code)
        shelve = alarm_pb2.ShelveAlarmRequest(
            alarm_id=oil.alarm_id, reason="Sensor work", minutes=30
        )
        shelved = (await world.stub.ShelveAlarm(shelve, metadata=as_user(), timeout=5)).alarm
        assert shelved.state == alarm_pb2.ALARM_STATE_SHELVED and shelved.shelve_reason
        no_reason = alarm_pb2.ShelveAlarmRequest(alarm_id=bearing.alarm_id, minutes=30)
        too_long = alarm_pb2.ShelveAlarmRequest(alarm_id=bearing.alarm_id, reason="x", minutes=1441)
        for bad in (no_reason, too_long):
            assert (
                await code_of(world.stub.ShelveAlarm(bad, metadata=as_user(), timeout=5))
                == grpc.StatusCode.INVALID_ARGUMENT
            )
        assert await expire_shelves(world.engine, START + timedelta(minutes=34)) == 0
        assert await expire_shelves(world.engine, START + timedelta(minutes=36)) == 1
        back = next(a for a in await world.standing() if a.alarm_id == oil.alarm_id)
        assert (back.state, back.shelved_until.seconds) == (alarm_pb2.ALARM_STATE_ACTIVE, 0)
        shelve2 = alarm_pb2.ShelveAlarmRequest(alarm_id=bearing.alarm_id, reason="Known", minutes=5)
        await world.stub.ShelveAlarm(shelve2, metadata=as_user(), timeout=5)
        unshelve = alarm_pb2.UnshelveAlarmRequest(alarm_id=bearing.alarm_id)
        unshelved = (await world.stub.UnshelveAlarm(unshelve, metadata=as_user(), timeout=5)).alarm
        assert unshelved.state == alarm_pb2.ALARM_STATE_ACTIVE

    with_service(scenario)


def test_each_caller_sees_and_acts_on_only_what_their_grants_cover() -> None:
    """RBAC-002, and GRP-004: another tenant's alarms do not exist for them."""

    async def scenario(world: World) -> None:
        await world.report("raise", turbine="dogger-east-t01")
        await world.report("raise", turbine="hornsea-west-t01")
        await world.report("raise", turbine="dogger-east-t01", tenant=OTHER)
        assert {a.turbine_id for a in await world.standing()} == {
            "dogger-east-t01", "hornsea-west-t01",
        }  # fmt: skip
        assert {a.turbine_id for a in await world.standing("u-dogger")} == {"dogger-east-t01"}
        assert await world.standing("u-dogger", wind_farm_id="hornsea-west") == []
        hornsea = next(a for a in await world.standing() if a.turbine_id == "hornsea-west-t01")
        ack = alarm_pb2.AcknowledgeAlarmRequest(alarm_id=hornsea.alarm_id)
        assert (
            await code_of(world.stub.AcknowledgeAlarm(ack, metadata=as_user("u-dogger"), timeout=5))
            == grpc.StatusCode.NOT_FOUND
        )
        # An auditor may read it, but not act on it.
        assert (
            await code_of(
                world.stub.AcknowledgeAlarm(ack, metadata=as_user("u-auditor"), timeout=5)
            )
            == grpc.StatusCode.PERMISSION_DENIED
        )
        other = alarm_pb2.GetAlarmRequest(alarm_id=hornsea.alarm_id)
        assert (
            await code_of(
                world.stub.GetAlarm(other, metadata=as_user("u-operator", OTHER), timeout=5)
            )
            == grpc.StatusCode.NOT_FOUND
        )

    with_service(scenario)


def test_only_the_device_route_may_report_alarms() -> None:
    async def scenario(world: World) -> None:
        payload = events_pb2.AlarmReported(turbine_id="t", code="X_Y", action="raise")
        forged = Event("alarm.reported", "e", TENANT, START, "alarm", payload, "", "")
        with pytest.raises(InvalidEventError):
            await handle(world.engine, forged)

    with_service(scenario)
