"""The API through a real gRPC server and SQL database, against a fake Keycloak admin API."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
import pytest
from fake_keycloak import FakeKeycloak
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.identity.v1 import identity_pb2, identity_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_events import parse_event
from windops_grpc import ServiceRuntime, Settings, UnavailableError, VerifiedIdentity, sign_identity
from windops_identity.keycloak import KeycloakAdmin, KeycloakSettings
from windops_identity.service import IdentityService
from windops_identity.store import METADATA, SERVICE_NAME
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
ADMIN = "u-admin"
SETTINGS = Settings(
    environment="test",
    version="test",
    identity_secret=SECRET,
    grpc_port=0,
    http_port=0,
    shutdown_grace_seconds=1,
)


@dataclass
class FakeAuthorizer:
    """Stands in for the Authorization Service, which has its own tests and cluster check."""

    allow: bool = True
    down: bool = False
    asked: list[tuple[str, str]] = field(default_factory=list)

    async def allowed(self, permission: str, target_tenant_id: str) -> bool:
        self.asked.append((permission, target_tenant_id))
        if self.down:
            raise UnavailableError("Permissions cannot be checked right now")
        return self.allow


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    keycloak: FakeKeycloak
    authorizer: FakeAuthorizer
    operator: str
    stranger: str


def as_user(user: str = ADMIN, tenant: str = TENANT, *, level: str | None = LEVEL_MFA) -> Any:
    identity = VerifiedIdentity(
        subject=user,
        tenant_id=tenant,
        user_id=user,
        auth_level=level,
        auth_time=int(time.time()) if level else None,
    )
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def audit_actions(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(select(OUTBOX_TABLE.c.subject, OUTBOX_TABLE.c.envelope))
        ).all()
    found = []
    for row in rows:
        event = parse_event(row.subject, row.envelope)
        found.append((event.tenant_id, event.payload.action))  # type: ignore[attr-defined]
    return found


def with_service(scenario: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        keycloak = FakeKeycloak()
        keycloak.add_tenant(TENANT)
        keycloak.add_tenant(OTHER)
        keycloak.add_user(TENANT, "admin@north-sea.example", user_id=ADMIN)
        operator = keycloak.add_user(TENANT, "operator@north-sea.example")
        stranger = keycloak.add_user(OTHER, "operator@atlas-wind.example")
        admin = KeycloakAdmin(
            KeycloakSettings("http://keycloak", "windops", "windops-identity-service", "s"),
            transport=keycloak.transport(),
        )
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        authorizer = FakeAuthorizer()

        def register(server: grpc.aio.Server) -> None:
            identity_pb2_grpc.add_IdentityServiceServicer_to_server(
                IdentityService(engine, admin, authorizer), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub: Any = identity_pb2_grpc.IdentityServiceStub(channel)
                await scenario(World(stub, engine, keycloak, authorizer, operator, stranger))
        finally:
            await runtime.stop()
            await admin.close()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        return error.code()
    return grpc.StatusCode.OK


def create(email: str = "new@north-sea.example", key: str = "") -> Any:
    return identity_pb2.CreateUserRequest(
        email=email, first_name="New", last_name="User", idempotency_key=key
    )


def test_it_asks_authorization_for_users_manage_in_the_callers_tenant() -> None:
    async def scenario(world: World) -> None:
        await world.stub.ListUsers(identity_pb2.ListUsersRequest(), metadata=as_user(), timeout=5)
        assert world.authorizer.asked == [("users.manage", "")]

    with_service(scenario)


def test_a_denied_caller_is_refused_before_keycloak_is_touched() -> None:
    async def scenario(world: World) -> None:
        world.authorizer.allow = False
        world.keycloak.calls.clear()
        code = await code_of(
            world.stub.ListUsers(identity_pb2.ListUsersRequest(), metadata=as_user(), timeout=5)
        )
        assert code is grpc.StatusCode.PERMISSION_DENIED
        assert world.keycloak.calls == []

    with_service(scenario)


def test_it_fails_closed_when_authorization_is_down() -> None:
    """SRS 7.7: deny the protected operation."""

    async def scenario(world: World) -> None:
        world.authorizer.down = True
        code = await code_of(
            world.stub.ListUsers(identity_pb2.ListUsersRequest(), metadata=as_user(), timeout=5)
        )
        assert code is grpc.StatusCode.UNAVAILABLE

    with_service(scenario)


@pytest.mark.parametrize("level", [LEVEL_PASSWORD, None])
def test_a_change_needs_a_recent_second_factor(level: str | None) -> None:
    async def scenario(world: World) -> None:
        metadata = as_user(level=level)
        for call in (
            world.stub.CreateUser(create(), metadata=metadata, timeout=5),
            world.stub.DisableUser(
                identity_pb2.DisableUserRequest(member_id=world.operator),
                metadata=metadata,
                timeout=5,
            ),
            world.stub.RevokeSessions(
                identity_pb2.RevokeSessionsRequest(member_id=world.operator),
                metadata=metadata,
                timeout=5,
            ),
        ):
            assert await code_of(call) is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_creating_a_user_puts_them_in_the_tenant_with_a_temporary_password() -> None:
    async def scenario(world: World) -> None:
        response = await world.stub.CreateUser(create(), metadata=as_user(), timeout=5)
        member = response.user.member_id
        assert response.temporary_password
        assert member in world.keycloak.members[world.keycloak.organizations[TENANT]]
        assert world.keycloak.users[member]["requiredActions"] == ["UPDATE_PASSWORD"]
        assert world.keycloak.passwords[member]["temporary"] is True
        assert world.keycloak.passwords[member]["value"] == response.temporary_password
        assert (TENANT, "identity.user.created") in await audit_actions(world.engine)

    with_service(scenario)


def test_a_retry_returns_the_same_user_and_never_the_password_again() -> None:
    """GRP-007."""

    async def scenario(world: World) -> None:
        first = await world.stub.CreateUser(create(key="k-1"), metadata=as_user(), timeout=5)
        again = await world.stub.CreateUser(create(key="k-1"), metadata=as_user(), timeout=5)
        assert again.user.member_id == first.user.member_id
        assert again.already_created and not again.temporary_password
        created = [
            u for u in world.keycloak.users.values() if u["email"] == "new@north-sea.example"
        ]
        assert len(created) == 1
        reused = create(email="other@north-sea.example", key="k-1")
        code = await code_of(world.stub.CreateUser(reused, metadata=as_user(), timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_an_email_in_use_anywhere_gets_the_same_answer() -> None:
    """The answer is the same for any tenant's user, so it reveals none of them."""

    async def scenario(world: World) -> None:
        for email in ("operator@north-sea.example", "operator@atlas-wind.example"):
            code = await code_of(
                world.stub.CreateUser(create(email), metadata=as_user(), timeout=5)
            )
            assert code is grpc.StatusCode.FAILED_PRECONDITION

    with_service(scenario)


def test_a_failed_tenant_membership_leaves_no_user_behind() -> None:
    async def scenario(world: World) -> None:
        world.keycloak.fail_adding_members = True
        code = await code_of(world.stub.CreateUser(create(), metadata=as_user(), timeout=5))
        assert code is grpc.StatusCode.UNAVAILABLE
        assert not [
            u for u in world.keycloak.users.values() if u["email"] == "new@north-sea.example"
        ]

    with_service(scenario)


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "spaces in@north-sea.example"])
def test_a_bad_email_is_refused(email: str) -> None:
    async def scenario(world: World) -> None:
        code = await code_of(world.stub.CreateUser(create(email), metadata=as_user(), timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_another_tenants_user_looks_like_no_user() -> None:
    """SEC-007: the same NOT_FOUND as for a user who does not exist."""

    async def scenario(world: World) -> None:
        metadata = as_user()
        for member in (world.stranger, "no-such-user"):
            for call in (
                world.stub.GetUser(
                    identity_pb2.GetUserRequest(member_id=member), metadata=metadata, timeout=5
                ),
                world.stub.DisableUser(
                    identity_pb2.DisableUserRequest(member_id=member), metadata=metadata, timeout=5
                ),
                world.stub.ListSessions(
                    identity_pb2.ListSessionsRequest(member_id=member), metadata=metadata, timeout=5
                ),
                world.stub.GetMfaStatus(
                    identity_pb2.GetMfaStatusRequest(member_id=member), metadata=metadata, timeout=5
                ),
            ):
                assert await code_of(call) is grpc.StatusCode.NOT_FOUND
        assert world.keycloak.users[world.stranger]["enabled"] is True

    with_service(scenario)


def test_listing_users_shows_only_this_tenant_in_pages() -> None:
    async def scenario(world: World) -> None:
        first = await world.stub.ListUsers(
            identity_pb2.ListUsersRequest(page_size=1), metadata=as_user(), timeout=5
        )
        second = await world.stub.ListUsers(
            identity_pb2.ListUsersRequest(page_size=1, page_token=first.next_page_token),
            metadata=as_user(),
            timeout=5,
        )
        emails = {user.email for user in [*first.users, *second.users]}
        assert emails == {"admin@north-sea.example", "operator@north-sea.example"}
        assert not second.next_page_token

    with_service(scenario)


def test_disabling_ends_sessions_and_is_audited_once() -> None:
    async def scenario(world: World) -> None:
        world.keycloak.sessions[world.operator] = [{"id": "s-1", "clients": {"c": "windops-web"}}]
        request = identity_pb2.DisableUserRequest(member_id=world.operator)
        disabled = await world.stub.DisableUser(request, metadata=as_user(), timeout=5)
        assert not disabled.user.enabled
        assert world.keycloak.sessions[world.operator] == []
        await world.stub.DisableUser(request, metadata=as_user(), timeout=5)
        actions = [a for a in await audit_actions(world.engine) if a[1] == "identity.user.disabled"]
        assert len(actions) == 1
        reactivated = await world.stub.ReactivateUser(
            identity_pb2.ReactivateUserRequest(member_id=world.operator),
            metadata=as_user(),
            timeout=5,
        )
        assert reactivated.user.enabled

    with_service(scenario)


def test_an_administrator_cannot_disable_themselves() -> None:
    async def scenario(world: World) -> None:
        request = identity_pb2.DisableUserRequest(member_id=ADMIN)
        code = await code_of(world.stub.DisableUser(request, metadata=as_user(), timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_sessions_are_listed_and_revoked() -> None:
    async def scenario(world: World) -> None:
        world.keycloak.sessions[world.operator] = [
            {
                "id": "s-1",
                "ipAddress": "10.0.0.1",
                "start": 1_767_225_600_000,
                "clients": {"a": "windops-web"},
            },
            {"id": "s-2", "clients": {}},
        ]
        listed = await world.stub.ListSessions(
            identity_pb2.ListSessionsRequest(member_id=world.operator),
            metadata=as_user(),
            timeout=5,
        )
        assert [s.session_id for s in listed.sessions] == ["s-1", "s-2"]
        assert list(listed.sessions[0].clients) == ["windops-web"]
        revoked = await world.stub.RevokeSessions(
            identity_pb2.RevokeSessionsRequest(member_id=world.operator),
            metadata=as_user(),
            timeout=5,
        )
        assert revoked.sessions_ended == 2
        assert (TENANT, "identity.sessions.revoked") in await audit_actions(world.engine)

    with_service(scenario)


def test_mfa_status_reads_the_users_credentials_and_pending_setup() -> None:
    async def scenario(world: World) -> None:
        world.keycloak.credentials[world.operator] = [{"type": "password"}, {"type": "otp"}]
        world.keycloak.users[world.operator]["requiredActions"] = ["CONFIGURE_RECOVERY_AUTHN_CODES"]
        status = await world.stub.GetMfaStatus(
            identity_pb2.GetMfaStatusRequest(member_id=world.operator),
            metadata=as_user(),
            timeout=5,
        )
        assert status.otp_configured
        assert not status.recovery_codes_configured
        assert status.setup_required

    with_service(scenario)


def test_a_keycloak_outage_refuses_the_change() -> None:
    """SRS 7.7: user changes are refused."""

    async def scenario(world: World) -> None:
        world.keycloak.fail_with = 503
        code = await code_of(
            world.stub.GetUser(
                identity_pb2.GetUserRequest(member_id=world.operator), metadata=as_user(), timeout=5
            )
        )
        assert code is grpc.StatusCode.UNAVAILABLE

    with_service(scenario)
