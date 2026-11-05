"""The API through a real gRPC server and a real SQL database, with signed identities."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.authorization.v1 import authorization_pb2, authorization_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_authorization import policy
from windops_authorization.service import AuthorizationService
from windops_authorization.store import METADATA, SERVICE_NAME, add_grant
from windops_events import parse_event
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, sign_identity
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
ADMIN, OPERATOR, PLATFORM = "u-admin", "u-operator", "u-platform"
SETTINGS = Settings(
    environment="test",
    version="test",
    identity_secret=SECRET,
    grpc_port=0,
    http_port=0,
    shutdown_grace_seconds=1,
)
Stub = authorization_pb2_grpc.AuthorizationServiceStub


def as_user(user: str, tenant: str = TENANT, *, level: str | None = LEVEL_MFA) -> Any:
    """Metadata for a user who used a second factor just now, unless a level says otherwise."""
    identity = VerifiedIdentity(
        subject=user,
        tenant_id=tenant,
        user_id=user,
        auth_level=level,
        auth_time=int(time.time()) if level else None,
    )
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def audit_actions(engine: AsyncEngine) -> list[tuple[str, str, str]]:
    """Return (tenant, action, outcome) for every audit record queued in the outbox."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(select(OUTBOX_TABLE.c.subject, OUTBOX_TABLE.c.envelope))
        ).all()
    found = []
    for row in rows:
        event = parse_event(row.subject, row.envelope)
        found.append((event.tenant_id, event.payload.action, event.payload.outcome))  # type: ignore[attr-defined]
    return found


def with_service(scenario: Callable[[Any, AsyncEngine], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
            await add_grant(
                connection, TENANT, ADMIN, policy.TENANT_ADMINISTRATOR,
                policy.Scope(), actor="test", now=_now(),
            )  # fmt: skip
            await add_grant(
                connection, policy.PLATFORM_TENANT, PLATFORM, policy.PLATFORM_ADMINISTRATOR,
                policy.Scope(), actor="test", now=_now(),
            )  # fmt: skip

        def register(server: grpc.aio.Server) -> None:
            authorization_pb2_grpc.add_AuthorizationServiceServicer_to_server(
                AuthorizationService(engine), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub: Any = Stub(channel)
                await scenario(stub, engine)
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def check(stub: Any, metadata: Any, permission: str, **scope: str) -> bool:
    request = authorization_pb2.CheckRequest(
        permission=permission, scope=authorization_pb2.Scope(**scope)
    )
    response = await stub.Check(request, metadata=metadata, timeout=5)
    return bool(response.allowed)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        return error.code()
    return grpc.StatusCode.OK


def test_a_user_with_no_role_is_denied_and_the_denial_is_audited() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        assert not await check(stub, as_user(OPERATOR), "turbine.read")
        assert (TENANT, "authorization.denied", "denied") in await audit_actions(engine)

    with_service(scenario)


def test_a_service_identity_with_no_user_is_denied() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        token = sign_identity(VerifiedIdentity("svc-x", TENANT), SECRET)
        response = await stub.Check(
            authorization_pb2.CheckRequest(permission="turbine.read"),
            metadata=(("x-windops-identity", token),),
            timeout=5,
        )
        assert not response.allowed

    with_service(scenario)


def test_an_administrator_grants_a_scoped_role_which_then_decides() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        granted = await stub.GrantRole(
            authorization_pb2.GrantRoleRequest(
                grantee_id=OPERATOR,
                role=policy.CONTROL_ROOM_OPERATOR,
                scope=authorization_pb2.Scope(wind_farm_id="farm-1"),
            ),
            metadata=as_user(ADMIN),
            timeout=5,
        )
        assert granted.grant.granted_by == ADMIN
        operator = as_user(OPERATOR)
        assert await check(stub, operator, "alarm.acknowledge", wind_farm_id="farm-1")
        assert not await check(stub, operator, "alarm.acknowledge", wind_farm_id="farm-2")
        assert not await check(stub, operator, "alarm.acknowledge")
        assert not await check(stub, operator, "maintenance.approve", wind_farm_id="farm-1")
        actions = await audit_actions(engine)
        assert (TENANT, "authorization.role.granted.control-room-operator", "succeeded") in actions

    with_service(scenario)


def test_granting_needs_a_recent_second_factor() -> None:
    """SEC-003."""

    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        request = authorization_pb2.GrantRoleRequest(
            grantee_id=OPERATOR, role=policy.CONTROL_ROOM_OPERATOR
        )
        for metadata in (as_user(ADMIN, level=LEVEL_PASSWORD), as_user(ADMIN, level=None)):
            code = await code_of(stub.GrantRole(request, metadata=metadata, timeout=5))
            assert code is grpc.StatusCode.PERMISSION_DENIED
        stale = VerifiedIdentity(
            ADMIN, TENANT, ADMIN, auth_level=LEVEL_MFA, auth_time=int(time.time()) - 3600
        )
        metadata = (("x-windops-identity", sign_identity(stale, SECRET)),)
        code = await code_of(stub.GrantRole(request, metadata=metadata, timeout=5))
        assert code is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_a_user_without_roles_manage_cannot_grant() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        request = authorization_pb2.GrantRoleRequest(
            grantee_id=OPERATOR, role=policy.TENANT_ADMINISTRATOR
        )
        code = await code_of(stub.GrantRole(request, metadata=as_user(OPERATOR), timeout=5))
        assert code is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_a_tenant_administrator_cannot_reach_another_tenant() -> None:
    """RBAC-001: naming another tenant does not move the caller there."""

    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        request = authorization_pb2.GrantRoleRequest(
            target_tenant_id=OTHER, grantee_id=ADMIN, role=policy.TENANT_ADMINISTRATOR
        )
        code = await code_of(stub.GrantRole(request, metadata=as_user(ADMIN), timeout=5))
        assert code is grpc.StatusCode.PERMISSION_DENIED
        response = await stub.Check(
            authorization_pb2.CheckRequest(permission="turbine.read", target_tenant_id=OTHER),
            metadata=as_user(ADMIN),
            timeout=5,
        )
        assert not response.allowed

    with_service(scenario)


def test_revoking_keeps_the_history_and_ends_the_access() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        admin = as_user(ADMIN)
        granted = await stub.GrantRole(
            authorization_pb2.GrantRoleRequest(grantee_id=OPERATOR, role=policy.AUDITOR),
            metadata=admin,
            timeout=5,
        )
        await stub.RevokeRole(
            authorization_pb2.RevokeRoleRequest(grant_id=granted.grant.grant_id),
            metadata=admin,
            timeout=5,
        )
        assert not await check(stub, as_user(OPERATOR), "audit.read")
        listed = await stub.ListRoleGrants(
            authorization_pb2.ListRoleGrantsRequest(grantee_id=OPERATOR, include_revoked=True),
            metadata=admin,
            timeout=5,
        )
        assert [grant.revoked_by for grant in listed.grants] == [ADMIN]

    with_service(scenario)


def test_the_last_tenant_administrator_cannot_be_removed() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        admin = as_user(ADMIN)
        listed = await stub.ListRoleGrants(
            authorization_pb2.ListRoleGrantsRequest(grantee_id=ADMIN), metadata=admin, timeout=5
        )
        request = authorization_pb2.RevokeRoleRequest(grant_id=listed.grants[0].grant_id)
        code = await code_of(stub.RevokeRole(request, metadata=admin, timeout=5))
        assert code is grpc.StatusCode.FAILED_PRECONDITION

    with_service(scenario)


def test_a_tenant_can_change_a_role_and_other_roles_keep_their_defaults() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        admin = as_user(ADMIN)
        await stub.GrantRole(
            authorization_pb2.GrantRoleRequest(
                grantee_id=OPERATOR, role=policy.CONTROL_ROOM_OPERATOR
            ),
            metadata=admin,
            timeout=5,
        )
        await stub.SetRolePermissions(
            authorization_pb2.SetRolePermissionsRequest(
                role=policy.CONTROL_ROOM_OPERATOR, permissions=["turbine.read"]
            ),
            metadata=admin,
            timeout=5,
        )
        assert await check(stub, as_user(OPERATOR), "turbine.read")
        assert not await check(stub, as_user(OPERATOR), "alarm.acknowledge")
        auditor = await stub.GetRolePermissions(
            authorization_pb2.GetRolePermissionsRequest(role=policy.AUDITOR),
            metadata=admin,
            timeout=5,
        )
        assert set(auditor.role_permissions.permissions) == policy.DEFAULT_MATRIX[policy.AUDITOR]
        assert auditor.role_permissions.customized

    with_service(scenario)


def test_a_tenant_cannot_give_itself_a_platform_permission() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        request = authorization_pb2.SetRolePermissionsRequest(
            role=policy.TENANT_ADMINISTRATOR,
            permissions=["roles.manage", "permissions.manage", "tenant.manage"],
        )
        code = await code_of(stub.SetRolePermissions(request, metadata=as_user(ADMIN), timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_break_glass_is_reasoned_time_limited_read_only_and_audited() -> None:
    """RBAC-003."""

    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        platform = as_user(PLATFORM, policy.PLATFORM_TENANT)
        reading = authorization_pb2.CheckRequest(
            permission="telemetry.read", target_tenant_id=TENANT
        )
        assert not (await stub.Check(reading, metadata=platform, timeout=5)).allowed
        short = authorization_pb2.StartBreakGlassRequest(target_tenant_id=TENANT, reason="because")
        code = await code_of(stub.StartBreakGlass(short, metadata=platform, timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT
        request = authorization_pb2.StartBreakGlassRequest(
            target_tenant_id=TENANT, reason="Investigating incident INC-4411 for the tenant"
        )
        no_step_up = as_user(PLATFORM, policy.PLATFORM_TENANT, level=LEVEL_PASSWORD)
        code = await code_of(stub.StartBreakGlass(request, metadata=no_step_up, timeout=5))
        assert code is grpc.StatusCode.PERMISSION_DENIED
        started = await stub.StartBreakGlass(request, metadata=platform, timeout=5)
        allowed = await stub.Check(reading, metadata=platform, timeout=5)
        assert allowed.allowed and allowed.break_glass
        change = authorization_pb2.CheckRequest(
            permission="maintenance.approve", target_tenant_id=TENANT
        )
        assert not (await stub.Check(change, metadata=platform, timeout=5)).allowed
        await stub.EndBreakGlass(
            authorization_pb2.EndBreakGlassRequest(grant_id=started.grant.grant_id),
            metadata=platform,
            timeout=5,
        )
        assert not (await stub.Check(reading, metadata=platform, timeout=5)).allowed
        actions = await audit_actions(engine)
        for action in ("started", "used", "ended"):
            assert (TENANT, f"authorization.break_glass.{action}", "succeeded") in actions

    with_service(scenario)


def test_only_a_platform_administrator_can_start_break_glass() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        request = authorization_pb2.StartBreakGlassRequest(
            target_tenant_id=OTHER, reason="Investigating incident INC-4411 for the tenant"
        )
        code = await code_of(stub.StartBreakGlass(request, metadata=as_user(ADMIN), timeout=5))
        assert code is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


@pytest.mark.parametrize("tenant", ["", "UPPER", "a" * 70, "../etc"])
def test_a_bad_tenant_id_is_refused(tenant: str) -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        identity = VerifiedIdentity(ADMIN, "north-sea", ADMIN)
        metadata = (("x-windops-identity", sign_identity(identity, SECRET)),)
        request = authorization_pb2.CheckRequest(
            permission="turbine.read", target_tenant_id=tenant or "x"
        )
        code = await code_of(stub.Check(request, metadata=metadata, timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_a_retry_with_the_same_key_returns_the_first_answer_and_changes_nothing() -> None:
    """GRP-007."""

    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        request = authorization_pb2.GrantRoleRequest(
            grantee_id=OPERATOR, role=policy.AUDITOR, idempotency_key="key-1"
        )
        first = await stub.GrantRole(request, metadata=as_user(ADMIN), timeout=5)
        again = await stub.GrantRole(request, metadata=as_user(ADMIN), timeout=5)
        assert again.grant.grant_id == first.grant.grant_id
        granted = [a for a in await audit_actions(engine) if a[1].endswith("granted.auditor")]
        assert len(granted) == 1

    with_service(scenario)


def test_a_key_reused_for_a_different_request_is_refused() -> None:
    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        first = authorization_pb2.GrantRoleRequest(
            grantee_id=OPERATOR, role=policy.AUDITOR, idempotency_key="key-2"
        )
        await stub.GrantRole(first, metadata=as_user(ADMIN), timeout=5)
        other = authorization_pb2.GrantRoleRequest(
            grantee_id="someone-else", role=policy.AUDITOR, idempotency_key="key-2"
        )
        code = await code_of(stub.GrantRole(other, metadata=as_user(ADMIN), timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_a_retry_still_needs_permission() -> None:
    """Knowing someone's key does not let another user replay their answer."""

    async def scenario(stub: Any, engine: AsyncEngine) -> None:
        request = authorization_pb2.GrantRoleRequest(
            grantee_id=OPERATOR, role=policy.AUDITOR, idempotency_key="key-3"
        )
        await stub.GrantRole(request, metadata=as_user(ADMIN), timeout=5)
        code = await code_of(stub.GrantRole(request, metadata=as_user(OPERATOR), timeout=5))
        assert code is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)
