"""Cases through a real gRPC server and SQL database, with stand-in Authorization and Asset
services. SQLite has no row-level security, so `make case-check` proves that on PostgreSQL."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.events.v1 import events_pb2
from windops.investigation.v1 import investigation_pb2 as pb
from windops.investigation.v1 import investigation_pb2_grpc
from windops_events import Event, InvalidEventError, parse_event
from windops_grpc import (
    NotFoundError,
    ServiceRuntime,
    Settings,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_investigation import case_rules
from windops_investigation.consumer import ACTOR, handle
from windops_investigation.service import InvestigationService
from windops_investigation.store import METADATA, SERVICE_NAME
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(
    environment="test", version="test", identity_secret=SECRET, grpc_port=0, http_port=0,
    shutdown_grace_seconds=1,
)  # fmt: skip
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
FARMS = {"dogger-east-t01": "dogger-east", "hornsea-west-t01": "hornsea-west"}


@dataclass
class FakeAuthorizer:
    """Grants (user, permission, wind farm). An empty wind farm means the whole tenant."""

    grants: set[tuple[str, str, str]] = field(default_factory=set)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        user = current_identity().user_id or ""
        return (user, permission, "") in self.grants or (
            bool(wind_farm_id) and (user, permission, wind_farm_id) in self.grants
        )


class FakeAssets:
    async def wind_farm_of(self, turbine_id: str) -> str:
        if turbine_id not in FARMS:
            raise NotFoundError("No such turbine")
        return FARMS[turbine_id]


@dataclass
class World:
    stub: Any
    engine: AsyncEngine

    async def detect(self, detection_id: str, rule_id: str, severity: str,
                     turbine: str = "dogger-east-t01", tenant: str = TENANT,
                     producer: str = "fault-detection") -> bool:  # fmt: skip
        payload = events_pb2.FaultDetected(
            detection_id=detection_id, turbine_id=turbine, rule_version="1",
            telemetry_snapshot_id=f"tsnap_{detection_id}", rule_id=rule_id, severity=severity,
            rule_content_hash="h" * 64,
        )  # fmt: skip
        return await handle(self.engine, Event("fault.detected", f"e-{detection_id}", tenant, NOW,
                                               producer, payload, "", ""))  # fmt: skip

    async def active(self, user: str = "u-operator", tenant: str = TENANT) -> list[Any]:
        listed = await self.stub.ListCases(pb.ListCasesRequest(), metadata=as_user(user, tenant),
                                           timeout=5)  # fmt: skip
        return list(listed.cases)

    async def events(self, subject: str) -> list[Any]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        return [parse_event(r.subject, r.envelope).payload for r in rows if r.subject == subject]


def as_user(user: str = "u-operator", tenant: str = TENANT) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=tenant, user_id=user,
                                auth_level=None, auth_time=int(time.time()))  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


def with_service(scenario: Callable[[World], Awaitable[None]], sources: Any = None) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        authorizer = FakeAuthorizer({
            ("u-operator", "investigation.read", ""), ("u-operator", "investigation.create", ""),
            ("u-engineer", "investigation.read", ""), ("u-engineer", "investigation.cancel", ""),
            ("u-auditor", "investigation.read", ""),
            ("u-dogger", "investigation.read", "dogger-east"),
        })  # fmt: skip

        def register(server: grpc.aio.Server) -> None:
            investigation_pb2_grpc.add_InvestigationServiceServicer_to_server(
                InvestigationService(
                    engine, authorizer, FakeAssets(), clock=lambda: NOW, sources=sources
                ),
                server,
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                await scenario(World(investigation_pb2_grpc.InvestigationServiceStub(channel),
                                     engine))  # fmt: skip
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_one_fault_makes_one_case_however_many_of_its_rules_find_it() -> None:
    """FR-FLT-004: the gearbox's rules group into one case; its severity is the highest found."""

    async def scenario(world: World) -> None:
        assert await world.detect("d1", "gearbox-oil-residual", "medium")
        assert not await world.detect("d1", "gearbox-oil-residual", "medium"), "a repeat"
        assert await world.detect("d2", "gearbox-bearing-residual", "high")
        assert await world.detect("d3", "gearbox-oil-alarm-and-residual", "high")
        (case,) = await world.active()
        assert (case.component_group, case.severity, case.detection_count, case.origin) == (
            "gearbox", "high", 3, "detection",
        )  # fmt: skip
        assert (case.created_by, case.case_rules_version) == (ACTOR, case_rules.VERSION)
        got = await world.stub.GetCase(pb.GetCaseRequest(case_id=case.case_id),
                                       metadata=as_user(), timeout=5)  # fmt: skip
        assert [d.detection_id for d in got.detections] == ["d1", "d2", "d3"]
        assert got.detections[0].telemetry_snapshot_id == "tsnap_d1"
        assert [h.action for h in got.history] == [
            "created",
            "severity_raised",
            "detection_attached",
        ]
        updates = await world.events("case.updated")
        assert [(u.action, u.severity, u.version) for u in updates] == [
            ("created", "medium", 1), ("severity_raised", "high", 2),
            ("detection_attached", "high", 3),
        ]  # fmt: skip

    with_service(scenario)


def test_another_component_or_turbine_is_another_case_and_a_low_one_opens_none() -> None:
    async def scenario(world: World) -> None:
        assert not await world.detect("g1", "vibration-gap", "low"), "low opens no case"
        await world.detect("d1", "gearbox-oil-residual", "medium")
        await world.detect("v1", "vibration-residual", "medium")
        await world.detect("h1", "gearbox-oil-residual", "high", turbine="hornsea-west-t01")
        await world.detect("s1", "main-bearing-residual", "medium")
        # A low detection joins the turbine's active case for its group, once there is one...
        assert await world.detect("s2", "gearbox-bearing-residual", "low")
        # ...and opens none for a group with no active case: a data gap is the sensors group.
        assert not await world.detect("s3", "main-bearing-gap", "low")
        groups = sorted((c.turbine_id, c.component_group) for c in await world.active())
        assert groups == [
            ("dogger-east-t01", "gearbox"), ("dogger-east-t01", "main_bearing"),
            ("dogger-east-t01", "vibration"), ("hornsea-west-t01", "gearbox"),
        ]  # fmt: skip

    with_service(scenario)


def test_after_a_case_is_cancelled_the_next_detection_opens_a_new_one() -> None:
    async def scenario(world: World) -> None:
        await world.detect("d1", "gearbox-oil-residual", "high")
        (case,) = await world.active()
        cancel = pb.CancelCaseRequest(
            case_id=case.case_id, reason="Sensor work", idempotency_key="c"
        )
        assert (
            await code_of(world.stub.CancelCase(cancel, metadata=as_user("u-operator"), timeout=5))
            == grpc.StatusCode.PERMISSION_DENIED
        ), "an operator may not cancel"
        done = await world.stub.CancelCase(cancel, metadata=as_user("u-engineer"), timeout=5)
        again = await world.stub.CancelCase(cancel, metadata=as_user("u-engineer"), timeout=5)
        assert done == again and done.case.status == "cancelled"
        assert done.case.cancel_reason == "Sensor work"
        await world.detect("d2", "gearbox-oil-residual", "high")
        (new,) = await world.active()
        assert new.case_id != case.case_id

    with_service(scenario)


def test_a_person_opens_a_case_of_their_own() -> None:
    """FR-INV-001: an authorized user starts an investigation."""

    async def scenario(world: World) -> None:
        await world.detect("d1", "gearbox-oil-residual", "high")
        request = pb.CreateCaseRequest(turbine_id="dogger-east-t01", title="Odd noise on site",
                                       severity="medium", idempotency_key="k")  # fmt: skip
        made = (await world.stub.CreateCase(request, metadata=as_user(), timeout=5)).case
        assert (made.origin, made.created_by, made.component_group, made.status) == (
            "user", "u-operator", "turbine", "open",
        )  # fmt: skip
        assert len(await world.active()) == 2, "a person's case is its own"
        for who, refused in (("u-auditor", grpc.StatusCode.PERMISSION_DENIED),
                             ("u-engineer", grpc.StatusCode.PERMISSION_DENIED)):  # fmt: skip
            other = pb.CreateCaseRequest(turbine_id="dogger-east-t01", title="x", severity="low")
            assert (
                await code_of(world.stub.CreateCase(other, metadata=as_user(who), timeout=5))
                == refused
            )
        unknown = pb.CreateCaseRequest(turbine_id="nowhere-t9", title="x", severity="low")
        assert (
            await code_of(world.stub.CreateCase(unknown, metadata=as_user(), timeout=5))
            == grpc.StatusCode.NOT_FOUND
        )

    with_service(scenario)


def test_each_reader_sees_only_what_their_grants_cover() -> None:
    async def scenario(world: World) -> None:
        await world.detect("d1", "gearbox-oil-residual", "high")
        await world.detect("h1", "gearbox-oil-residual", "high", turbine="hornsea-west-t01")
        await world.detect("x1", "gearbox-oil-residual", "high", tenant=OTHER)
        assert len(await world.active()) == 2
        assert (
            await code_of(
                world.stub.ListCases(pb.ListCasesRequest(), metadata=as_user("u-dogger"), timeout=5)
            )
            == grpc.StatusCode.PERMISSION_DENIED
        )
        mine = await world.stub.ListCases(pb.ListCasesRequest(turbine_id="dogger-east-t01"),
                                          metadata=as_user("u-dogger"), timeout=5)  # fmt: skip
        assert [c.turbine_id for c in mine.cases] == ["dogger-east-t01"]
        hornsea = next(c for c in await world.active() if c.turbine_id == "hornsea-west-t01")
        get = pb.GetCaseRequest(case_id=hornsea.case_id)
        assert (
            await code_of(world.stub.GetCase(get, metadata=as_user("u-dogger"), timeout=5))
            == grpc.StatusCode.NOT_FOUND
        )
        assert (
            await code_of(world.stub.GetCase(get, metadata=as_user("u-operator", OTHER), timeout=5))
            == grpc.StatusCode.NOT_FOUND
        )

    with_service(scenario)


def test_only_the_fault_detection_service_may_report_a_detection() -> None:
    async def scenario(world: World) -> None:
        with pytest.raises(InvalidEventError):
            await world.detect("d1", "gearbox-oil-residual", "high", producer="alarm")

    with_service(scenario)
