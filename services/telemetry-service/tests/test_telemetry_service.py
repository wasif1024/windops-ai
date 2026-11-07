"""The device credential API through a real gRPC server and SQL database, with stand-ins for the
Authorization and Asset services."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import grpc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.telemetry.v1 import telemetry_pb2, telemetry_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_events import parse_event
from windops_grpc import (
    NotFoundError,
    ServiceRuntime,
    Settings,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_outbox import OUTBOX_TABLE, create_outbox_table
from windops_telemetry import credentials, partitions
from windops_telemetry.service import TelemetryService
from windops_telemetry.store import DEVICE_CREDENTIALS, METADATA, SERVICE_NAME

SECRET = "x" * 32
TENANT = "north-sea"
SETTINGS = Settings(
    environment="test",
    version="test",
    identity_secret=SECRET,
    grpc_port=0,
    http_port=0,
    shutdown_grace_seconds=1,
)
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


class FakeTurbines:
    async def wind_farm_of(self, turbine_id: str) -> str:
        if turbine_id not in FARMS:
            raise NotFoundError("No such turbine")
        return FARMS[turbine_id]


@dataclass
class World:
    stub: Any
    engine: AsyncEngine


def as_user(user: str = "u-admin", *, level: str | None = LEVEL_MFA) -> Any:
    identity = VerifiedIdentity(
        subject=user,
        tenant_id=TENANT,
        user_id=user,
        auth_level=level,
        auth_time=int(time.time()) if level else None,
    )
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


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
            {("u-admin", "asset.manage", ""), ("u-dogger", "asset.manage", "dogger-east")}
        )

        def register(server: grpc.aio.Server) -> None:
            telemetry_pb2_grpc.add_TelemetryServiceServicer_to_server(
                TelemetryService(engine, authorizer, FakeTurbines()), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                await scenario(World(telemetry_pb2_grpc.TelemetryServiceStub(channel), engine))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def create(turbine: str = "dogger-east-t01", key: str = "") -> Any:
    return telemetry_pb2.CreateDeviceCredentialRequest(turbine_id=turbine, idempotency_key=key)


def test_an_administrator_makes_a_credential_whose_token_is_shown_once() -> None:
    """FR-TEL-005: a retry gets the credential back, never its secret."""

    async def scenario(world: World) -> None:
        first = await world.stub.CreateDeviceCredential(
            create(key="k1"), metadata=as_user(), timeout=5
        )
        shown = credentials.presented(f"Bearer {first.token}")
        assert shown is not None
        assert (shown.tenant_id, shown.device_id) == (TENANT, first.credential.device_id)
        assert first.credential.created_by == "u-admin"
        retry = await world.stub.CreateDeviceCredential(
            create(key="k1"), metadata=as_user(), timeout=5
        )
        assert retry.credential.device_id == first.credential.device_id
        assert retry.token == ""
        async with world.engine.connect() as connection:
            stored = (await connection.execute(select(DEVICE_CREDENTIALS))).all()
            envelopes = (await connection.execute(select(OUTBOX_TABLE))).all()
        assert len(stored) == 1
        assert credentials.matches(shown.secret, stored[0].secret_hash)
        assert shown.secret not in stored[0].secret_hash
        assert len(envelopes) == 1
        event: Any = parse_event(envelopes[0].subject, envelopes[0].envelope)
        assert event.payload.action == "telemetry.device_credential.created"
        assert event.payload.resource_id == first.credential.device_id
        listed = await world.stub.ListDeviceCredentials(
            telemetry_pb2.ListDeviceCredentialsRequest(), metadata=as_user(), timeout=5
        )
        assert [c.device_id for c in listed.credentials] == [first.credential.device_id]

    with_service(scenario)


def test_making_a_credential_needs_the_permission_for_its_wind_farm_and_a_second_factor() -> None:
    """RBAC-002 and step-up: a wind farm grant covers only that farm's turbines."""

    async def scenario(world: World) -> None:
        stub = world.stub
        assert (
            await code_of(
                stub.CreateDeviceCredential(create(), metadata=as_user("u-dogger"), timeout=5)
            )
            == grpc.StatusCode.OK
        )
        for metadata, turbine in (
            (as_user("u-dogger"), "hornsea-west-t01"),
            (as_user("u-nobody"), "dogger-east-t01"),
            (as_user(level=LEVEL_PASSWORD), "dogger-east-t01"),
        ):
            assert (
                await code_of(
                    stub.CreateDeviceCredential(create(turbine), metadata=metadata, timeout=5)
                )
                == grpc.StatusCode.PERMISSION_DENIED
            )
        assert (
            await code_of(
                stub.CreateDeviceCredential(create("nowhere-t09"), metadata=as_user(), timeout=5)
            )
            == grpc.StatusCode.NOT_FOUND
        )
        assert (
            await code_of(
                stub.ListDeviceCredentials(
                    telemetry_pb2.ListDeviceCredentialsRequest(),
                    metadata=as_user("u-nobody"),
                    timeout=5,
                )
            )
            == grpc.StatusCode.PERMISSION_DENIED
        )

    with_service(scenario)


def test_a_revoked_credential_is_listed_only_when_asked_and_cannot_be_revoked_twice() -> None:
    async def scenario(world: World) -> None:
        made = await world.stub.CreateDeviceCredential(create(), metadata=as_user(), timeout=5)
        device = made.credential.device_id
        revoke = telemetry_pb2.RevokeDeviceCredentialRequest(device_id=device, idempotency_key="r")
        revoked = await world.stub.RevokeDeviceCredential(revoke, metadata=as_user(), timeout=5)
        assert revoked.credential.HasField("revoked_at")
        again = await world.stub.RevokeDeviceCredential(revoke, metadata=as_user(), timeout=5)
        assert again.credential.device_id == device
        assert (
            await code_of(
                world.stub.RevokeDeviceCredential(
                    telemetry_pb2.RevokeDeviceCredentialRequest(device_id=device),
                    metadata=as_user(),
                    timeout=5,
                )
            )
            == grpc.StatusCode.FAILED_PRECONDITION
        )
        working = await world.stub.ListDeviceCredentials(
            telemetry_pb2.ListDeviceCredentialsRequest(), metadata=as_user(), timeout=5
        )
        everything = await world.stub.ListDeviceCredentials(
            telemetry_pb2.ListDeviceCredentialsRequest(include_revoked=True),
            metadata=as_user(),
            timeout=5,
        )
        assert (len(working.credentials), len(everything.credentials)) == (0, 1)
        assert (
            await code_of(
                world.stub.RevokeDeviceCredential(
                    telemetry_pb2.RevokeDeviceCredentialRequest(device_id="dev_0000000000000000"),
                    metadata=as_user(),
                    timeout=5,
                )
            )
            == grpc.StatusCode.NOT_FOUND
        )

    with_service(scenario)


def test_reading_telemetry_says_it_arrives_later() -> None:
    async def scenario(world: World) -> None:
        call = world.stub.CreateTelemetrySnapshot(
            telemetry_pb2.CreateTelemetrySnapshotRequest(), metadata=as_user(), timeout=5
        )
        assert await code_of(call) == grpc.StatusCode.UNIMPLEMENTED

    with_service(scenario)


def test_partitions_are_named_by_day_and_skipped_off_postgresql() -> None:
    assert partitions.name_for(date(2026, 1, 5)) == "operational_samples_20260105"

    async def run() -> list[str]:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            return await partitions.ensure(engine)
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == []


def test_the_migration_creates_every_column_the_service_writes() -> None:
    """The migration spells out its SQL, so this keeps it and the code's table the same."""
    from pathlib import Path

    from windops_telemetry.store import OPERATIONAL_SAMPLES

    versions = Path(__file__).parents[1] / "src" / "windops_telemetry" / "migrations" / "versions"
    text = "".join(path.read_text() for path in versions.glob("rev_*.py"))
    missing = [column.name for column in OPERATIONAL_SAMPLES.c if f'"{column.name} ' not in text]
    assert missing == []
