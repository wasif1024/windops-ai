"""The API through a real gRPC server and SQL database, with a stand-in Authorization Service.

SQLite has no row-level security, so `make asset-check` proves that part against PostgreSQL.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops_asset.service import AssetService, Readable
from windops_asset.store import METADATA, SERVICE_NAME
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_events import parse_event
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, current_identity, sign_identity
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(
    environment="test",
    version="test",
    identity_secret=SECRET,
    grpc_port=0,
    http_port=0,
    shutdown_grace_seconds=1,
)
FARM = asset_pb2.CreateWindFarmRequest(
    wind_farm_id="dogger-east",
    name="Dogger East",
    country="GB",
    location=asset_pb2.Location(latitude=54.8, longitude=2.1),
    time_zone="Europe/London",
    offshore=True,
)
MODEL = asset_pb2.TurbineModel(
    manufacturer="Synthetic Turbines",
    model="ST-8.0",
    rated_power_kw=8000,
    rotor_diameter_m=167,
    hub_height_m=109,
)


def turbine(turbine_id: str = "dogger-east-t01", farm: str = "dogger-east") -> Any:
    return asset_pb2.CreateTurbineRequest(
        turbine_id=turbine_id,
        wind_farm_id=farm,
        name=turbine_id.upper(),
        location=asset_pb2.Location(latitude=54.81, longitude=2.11),
        model=MODEL,
        serial_number="SN-1",
        commissioned_on="2024-05-17",
    )


@dataclass
class FakeAuthorizer:
    """Grants (user, permission, wind farm). An empty wind farm means the whole tenant."""

    grants: set[tuple[str, str, str]] = field(default_factory=set)
    turbine_grants: set[tuple[str, str]] = field(default_factory=set)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        user = current_identity().user_id or ""
        return (
            (user, permission, "") in self.grants
            or (bool(wind_farm_id) and (user, permission, wind_farm_id) in self.grants)
            or (permission == "turbine.read" and (user, turbine_id) in self.turbine_grants)
        )

    async def readable(self) -> Readable:
        user = current_identity().user_id or ""
        farms = {
            farm
            for who, permission, farm in self.grants
            if who == user and permission == "turbine.read"
        }
        return Readable(
            everything="" in farms,
            wind_farms=frozenset(farm for farm in farms if farm),
            turbines=frozenset(turbine for who, turbine in self.turbine_grants if who == user),
        )


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    authorizer: FakeAuthorizer


def as_user(user: str = "u-admin", tenant: str = TENANT, *, level: str | None = LEVEL_MFA) -> Any:
    identity = VerifiedIdentity(
        subject=user,
        tenant_id=tenant,
        user_id=user,
        auth_level=level,
        auth_time=int(time.time()) if level else None,
    )
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def events(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(select(OUTBOX_TABLE.c.subject, OUTBOX_TABLE.c.envelope))
        ).all()
    return [(row.subject, parse_event(row.subject, row.envelope)) for row in rows]


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
                ("u-admin", "asset.manage", ""),
                ("u-admin", "turbine.read", ""),
                ("u-atlas-admin", "asset.manage", ""),
                ("u-atlas-admin", "turbine.read", ""),
                ("u-operator", "turbine.read", "dogger-east"),
            },
            {("u-one-turbine", "hornsea-t01")},
        )

        def register(server: grpc.aio.Server) -> None:
            asset_pb2_grpc.add_AssetServiceServicer_to_server(
                AssetService(engine, authorizer), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                await scenario(World(asset_pb2_grpc.AssetServiceStub(channel), engine, authorizer))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


async def seed(stub: Any) -> None:
    await stub.CreateWindFarm(FARM, metadata=as_user(), timeout=5)
    hornsea = asset_pb2.CreateWindFarmRequest()
    hornsea.CopyFrom(FARM)
    hornsea.wind_farm_id, hornsea.name = "hornsea", "Hornsea"
    await stub.CreateWindFarm(hornsea, metadata=as_user(), timeout=5)
    for request in (turbine(), turbine("dogger-east-t02"), turbine("hornsea-t01", "hornsea")):
        await stub.CreateTurbine(request, metadata=as_user(), timeout=5)


def test_an_administrator_creates_a_wind_farm_and_turbines_which_are_read_back() -> None:
    """FR-AST-001."""

    async def scenario(world: World) -> None:
        await seed(world.stub)
        got = await world.stub.GetTurbine(
            asset_pb2.GetTurbineRequest(turbine_id="dogger-east-t01"), metadata=as_user(), timeout=5
        )
        assert got.turbine.model.rated_power_kw == 8000
        assert (got.turbine.status, got.turbine.version) == ("operating", 1)
        farms = await world.stub.ListWindFarms(
            asset_pb2.ListWindFarmsRequest(), metadata=as_user(), timeout=5
        )
        assert [farm.wind_farm_id for farm in farms.wind_farms] == ["dogger-east", "hornsea"]

    with_service(scenario)


def test_a_scoped_reader_sees_only_their_wind_farm_or_turbine() -> None:
    """RBAC-002: a grant for one wind farm, or one turbine, shows only that."""

    async def scenario(world: World) -> None:
        await seed(world.stub)
        operator = as_user("u-operator")
        listed = await world.stub.ListTurbines(
            asset_pb2.ListTurbinesRequest(), metadata=operator, timeout=5
        )
        assert [item.turbine_id for item in listed.turbines] == [
            "dogger-east-t01",
            "dogger-east-t02",
        ]
        hidden = asset_pb2.GetTurbineRequest(turbine_id="hornsea-t01")
        assert (
            await code_of(world.stub.GetTurbine(hidden, metadata=operator, timeout=5))
            is grpc.StatusCode.PERMISSION_DENIED
        )
        one = as_user("u-one-turbine")
        farms = await world.stub.ListWindFarms(
            asset_pb2.ListWindFarmsRequest(), metadata=one, timeout=5
        )
        assert [farm.wind_farm_id for farm in farms.wind_farms] == ["hornsea"]
        mine = await world.stub.ListTurbines(
            asset_pb2.ListTurbinesRequest(), metadata=one, timeout=5
        )
        assert [item.turbine_id for item in mine.turbines] == ["hornsea-t01"]

    with_service(scenario)


def test_another_tenants_assets_look_like_assets_that_do_not_exist() -> None:
    """SEC-007: the tenant comes from the verified identity, never the request."""

    async def scenario(world: World) -> None:
        await seed(world.stub)
        theirs = as_user("u-atlas-admin", OTHER)
        request = asset_pb2.GetTurbineRequest(turbine_id="dogger-east-t01")
        assert (
            await code_of(world.stub.GetTurbine(request, metadata=theirs, timeout=5))
            is grpc.StatusCode.NOT_FOUND
        )
        listed = await world.stub.ListTurbines(
            asset_pb2.ListTurbinesRequest(), metadata=theirs, timeout=5
        )
        assert not listed.turbines
        status = asset_pb2.SetTurbineStatusRequest(
            turbine_id="dogger-east-t01", status="stopped", reason="x"
        )
        assert (
            await code_of(world.stub.SetTurbineStatus(status, metadata=theirs, timeout=5))
            is grpc.StatusCode.NOT_FOUND
        )

    with_service(scenario)


def test_changes_need_asset_manage_and_a_recent_second_factor() -> None:
    """SEC-003."""

    async def scenario(world: World) -> None:
        refused = await code_of(
            world.stub.CreateWindFarm(FARM, metadata=as_user("u-operator"), timeout=5)
        )
        assert refused is grpc.StatusCode.PERMISSION_DENIED
        try:
            await world.stub.CreateWindFarm(FARM, metadata=as_user(level=LEVEL_PASSWORD), timeout=5)
        except grpc.aio.AioRpcError as error:
            assert error.code() is grpc.StatusCode.PERMISSION_DENIED
            assert "second factor" in (error.details() or "")
        else:
            raise AssertionError("A change without a second factor was allowed")

    with_service(scenario)


def test_status_and_components_raise_the_turbines_version_and_announce_it() -> None:
    """FR-AST-002 and SRS 8.5: a material change is announced, so old evidence can go stale."""

    async def scenario(world: World) -> None:
        await seed(world.stub)
        stopped = await world.stub.SetTurbineStatus(
            asset_pb2.SetTurbineStatusRequest(
                turbine_id="dogger-east-t01", status="stopped", reason="Grid fault"
            ),
            metadata=as_user(),
            timeout=5,
        )
        assert (stopped.turbine.status, stopped.turbine.version) == ("stopped", 2)
        gearbox = await world.stub.AddComponent(
            asset_pb2.AddComponentRequest(
                turbine_id="dogger-east-t01", kind="gearbox", name="Gearbox"
            ),
            metadata=as_user(),
            timeout=5,
        )
        bearing = await world.stub.AddComponent(
            asset_pb2.AddComponentRequest(
                turbine_id="dogger-east-t01",
                parent_component_id=gearbox.component.component_id,
                kind="gearbox_bearing",
                name="High-speed shaft bearing",
            ),
            metadata=as_user(),
            timeout=5,
        )
        await world.stub.SetComponentStatus(
            asset_pb2.SetComponentStatusRequest(
                component_id=bearing.component.component_id,
                status="degraded",
                reason="Rising vibration",
            ),
            metadata=as_user(),
            timeout=5,
        )
        got = await world.stub.GetTurbine(
            asset_pb2.GetTurbineRequest(turbine_id="dogger-east-t01"), metadata=as_user(), timeout=5
        )
        assert got.turbine.version == 5
        parts = {part.kind: part for part in got.components}
        assert parts["gearbox_bearing"].parent_component_id == parts["gearbox"].component_id
        assert parts["gearbox_bearing"].status == "degraded"
        changed = [
            event.payload.version
            for subject, event in await events(world.engine)
            if subject == "evidence.changed" and event.payload.record_id == "dogger-east-t01"
        ]
        assert changed == ["1", "2", "3", "4", "5"]

    with_service(scenario)


def test_a_part_goes_only_under_a_part_of_the_same_turbine() -> None:
    async def scenario(world: World) -> None:
        await seed(world.stub)
        gearbox = await world.stub.AddComponent(
            asset_pb2.AddComponentRequest(
                turbine_id="dogger-east-t01", kind="gearbox", name="Gearbox"
            ),
            metadata=as_user(),
            timeout=5,
        )
        elsewhere = asset_pb2.AddComponentRequest(
            turbine_id="dogger-east-t02",
            parent_component_id=gearbox.component.component_id,
            kind="gearbox_bearing",
            name="Bearing",
        )
        assert (
            await code_of(world.stub.AddComponent(elsewhere, metadata=as_user(), timeout=5))
            is grpc.StatusCode.INVALID_ARGUMENT
        )

    with_service(scenario)


@pytest.mark.parametrize(
    "request_message",
    [
        asset_pb2.CreateWindFarmRequest(
            wind_farm_id="Bad ID", name="x", country="GB", time_zone="Europe/London"
        ),
        asset_pb2.CreateWindFarmRequest(
            wind_farm_id="ok-id", name="x", country="gb", time_zone="Europe/London"
        ),
        asset_pb2.CreateWindFarmRequest(
            wind_farm_id="ok-id", name="x", country="GB", time_zone="Mars/Olympus"
        ),
        asset_pb2.CreateWindFarmRequest(
            wind_farm_id="ok-id",
            name="x",
            country="GB",
            time_zone="Europe/London",
            location=asset_pb2.Location(latitude=91, longitude=0),
        ),
    ],
    ids=["id", "country", "time_zone", "latitude"],
)
def test_an_invalid_wind_farm_is_refused(request_message: Any) -> None:
    async def scenario(world: World) -> None:
        code = await code_of(
            world.stub.CreateWindFarm(request_message, metadata=as_user(), timeout=5)
        )
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_a_status_other_than_operating_needs_a_reason() -> None:
    async def scenario(world: World) -> None:
        await seed(world.stub)
        silent = asset_pb2.SetTurbineStatusRequest(turbine_id="dogger-east-t01", status="fault")
        assert (
            await code_of(world.stub.SetTurbineStatus(silent, metadata=as_user(), timeout=5))
            is grpc.StatusCode.INVALID_ARGUMENT
        )
        unknown = asset_pb2.SetTurbineStatusRequest(
            turbine_id="dogger-east-t01", status="exploded", reason="x"
        )
        assert (
            await code_of(world.stub.SetTurbineStatus(unknown, metadata=as_user(), timeout=5))
            is grpc.StatusCode.INVALID_ARGUMENT
        )

    with_service(scenario)


def test_a_retry_with_the_same_key_changes_nothing_more() -> None:
    """GRP-007."""

    async def scenario(world: World) -> None:
        await seed(world.stub)
        request = asset_pb2.SetTurbineStatusRequest(
            turbine_id="dogger-east-t01",
            status="maintenance",
            reason="Annual service",
            idempotency_key="k-1",
        )
        first = await world.stub.SetTurbineStatus(request, metadata=as_user(), timeout=5)
        again = await world.stub.SetTurbineStatus(request, metadata=as_user(), timeout=5)
        assert first.turbine.version == again.turbine.version == 2
        other = asset_pb2.SetTurbineStatusRequest(
            turbine_id="dogger-east-t01", status="operating", idempotency_key="k-1"
        )
        assert (
            await code_of(world.stub.SetTurbineStatus(other, metadata=as_user(), timeout=5))
            is grpc.StatusCode.FAILED_PRECONDITION
        )

    with_service(scenario)


def test_creating_the_same_wind_farm_twice_is_harmless_but_a_different_one_conflicts() -> None:
    async def scenario(world: World) -> None:
        first = await world.stub.CreateWindFarm(FARM, metadata=as_user(), timeout=5)
        again = await world.stub.CreateWindFarm(FARM, metadata=as_user(), timeout=5)
        assert (first.created, again.created) == (True, False)
        different = asset_pb2.CreateWindFarmRequest()
        different.CopyFrom(FARM)
        different.name = "Another"
        assert (
            await code_of(world.stub.CreateWindFarm(different, metadata=as_user(), timeout=5))
            is grpc.StatusCode.FAILED_PRECONDITION
        )

    with_service(scenario)


def test_changes_are_audited() -> None:
    async def scenario(world: World) -> None:
        await seed(world.stub)
        actions = [
            event.payload.action
            for subject, event in await events(world.engine)
            if subject.startswith("audit.")
        ]
        assert actions.count("asset.wind_farm.created") == 2
        assert actions.count("asset.turbine.created") == 3

    with_service(scenario)


def test_the_demo_seed_is_complete_and_safe_to_repeat() -> None:
    """Every turbine gets the whole tree, gearbox and bearings included (FR-AST-002)."""
    from datetime import UTC, datetime

    from windops_asset.seed import FARMS, PARTS, seed_farm
    from windops_asset.store import COMPONENTS

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
            first = await seed_farm(connection, FARMS[0], datetime.now(UTC))
            again = await seed_farm(connection, FARMS[0], datetime.now(UTC))
            kinds = set((await connection.execute(select(COMPONENTS.c.kind))).scalars())
        await engine.dispose()
        assert first == 1 + FARMS[0].turbines * (1 + len(PARTS))
        assert again == 0
        assert {"gearbox", "gearbox_bearing", "main_bearing", "generator_bearing"} <= kinds

    asyncio.run(run())
