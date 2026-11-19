"""The API through a real gRPC server and SQL database, against a fake Keycloak admin API."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fake_keycloak import FakeKeycloak
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.identity.v1 import identity_pb2, identity_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_events import parse_event
from windops_grpc import ServiceRuntime, Settings, UnavailableError, VerifiedIdentity, sign_identity
from windops_identity.agents import SigningKey
from windops_identity.keycloak import KeycloakAdmin, KeycloakSettings
from windops_identity.service import IdentityService
from windops_identity.store import METADATA, SERVICE_NAME
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
AGENT_ISSUER = "windops-identity-agents"
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
    # The certificate the next call presents, and the agent token key's public JWKS.
    peer: list[str] = field(default_factory=lambda: ["windops-investigation"])
    jwks: dict[str, Any] = field(default_factory=dict)


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
        peer = ["windops-investigation"]
        key = SigningKey.from_pem(
            ec.generate_private_key(ec.SECP256R1()).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )  # fmt: skip

        def register(server: grpc.aio.Server) -> None:
            identity_pb2_grpc.add_IdentityServiceServicer_to_server(
                IdentityService(
                    engine, admin, authorizer, signing_key=key, agent_issuer=AGENT_ISSUER,
                    peer=lambda: peer[0],
                ),
                server,
            )  # fmt: skip

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub: Any = identity_pb2_grpc.IdentityServiceStub(channel)
                await scenario(
                    World(stub, engine, keycloak, authorizer, operator, stranger, peer, key.jwks())
                )
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


def test_platform_staff_create_and_suspend_a_tenants_organization() -> None:
    async def scenario(world: World) -> None:
        platform = as_user("u-platform", "windops-platform")
        created = await world.stub.CreateOrganization(
            identity_pb2.CreateOrganizationRequest(
                target_tenant_id="gale-force", name="Gale Force"
            ),
            metadata=platform,
            timeout=5,
        )
        assert created.created
        assert world.authorizer.asked[-1] == ("tenant.manage", "gale-force")
        again = await world.stub.CreateOrganization(
            identity_pb2.CreateOrganizationRequest(
                target_tenant_id="gale-force", name="Gale Force"
            ),
            metadata=platform,
            timeout=5,
        )
        assert again.organization_id == created.organization_id and not again.created
        await world.stub.SetOrganizationEnabled(
            identity_pb2.SetOrganizationEnabledRequest(
                target_tenant_id="gale-force", enabled=False
            ),
            metadata=platform,
            timeout=5,
        )
        assert "gale-force" in world.keycloak.disabled
        actions = {a for _, a in await audit_actions(world.engine)}
        assert {"identity.organization.created", "identity.organization.disabled"} <= actions

    with_service(scenario)


def test_a_different_name_for_an_existing_tenant_is_a_conflict() -> None:
    async def scenario(world: World) -> None:
        request = identity_pb2.CreateOrganizationRequest(target_tenant_id=OTHER, name="Not Atlas")
        code = await code_of(
            world.stub.CreateOrganization(
                request, metadata=as_user("u-p", "windops-platform"), timeout=5
            )
        )
        assert code is grpc.StatusCode.FAILED_PRECONDITION

    with_service(scenario)


def test_nobody_suspends_their_own_tenant() -> None:
    async def scenario(world: World) -> None:
        request = identity_pb2.SetOrganizationEnabledRequest(enabled=False)
        code = await code_of(
            world.stub.SetOrganizationEnabled(request, metadata=as_user(), timeout=5)
        )
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


AGENT = "windops-agent-runtime"


def own_token(world: World, user: str = ADMIN, tenant: str = TENANT) -> str:
    world.keycloak.tokens["user-token"] = {"active": True, "sub": user, "tenant_id": tenant}
    return "user-token"


def exchange(token: str, audience: str = AGENT, scopes: tuple[str, ...] = ()) -> Any:
    return identity_pb2.ExchangeTokenRequest(
        subject_token=token, audience=audience, scopes=list(scopes)
    )


def test_a_user_exchanges_their_own_token_for_one_allowed_service() -> None:
    """SEC-008."""

    async def scenario(world: World) -> None:
        response = await world.stub.ExchangeToken(
            exchange(own_token(world)), metadata=as_user(), timeout=5
        )
        assert response.access_token == f"exchanged-for-{AGENT}"
        assert (response.expires_in_seconds, response.audience) == (120, AGENT)
        sent = world.keycloak.exchanges[0]
        assert (sent["audience"], sent["scope"]) == (AGENT, f"{AGENT}-audience")
        assert (TENANT, f"identity.token.exchanged.{AGENT}") in await audit_actions(world.engine)

    with_service(scenario)


@pytest.mark.parametrize(
    "owner",
    [
        {"active": True, "sub": "someone-else", "tenant_id": TENANT},
        {"active": True, "sub": ADMIN, "tenant_id": OTHER},
        {"active": False, "sub": ADMIN, "tenant_id": TENANT},
        {"active": True, "sub": ADMIN},
    ],
)
def test_only_the_callers_own_current_token_is_exchanged(owner: dict[str, Any]) -> None:
    """A service holding another user's token cannot turn it into one for an agent."""

    async def scenario(world: World) -> None:
        world.keycloak.tokens["stolen"] = owner
        code = await code_of(
            world.stub.ExchangeToken(exchange("stolen"), metadata=as_user(), timeout=5)
        )
        assert code is grpc.StatusCode.PERMISSION_DENIED
        assert world.keycloak.exchanges == []

    with_service(scenario)


@pytest.mark.parametrize("audience", ["windops-admin", "windops-web", "", "windops-agent-runtime "])
def test_only_allowed_services_can_receive_an_exchanged_token(audience: str) -> None:
    async def scenario(world: World) -> None:
        call = world.stub.ExchangeToken(
            exchange(own_token(world), audience), metadata=as_user(), timeout=5
        )
        assert await code_of(call) is grpc.StatusCode.INVALID_ARGUMENT
        assert world.keycloak.exchanges == []

    with_service(scenario)


DOCUMENTATION = "windops-mcp-documentation"
SEARCH = "mcp.documentation.search_documents"


def test_a_token_for_an_mcp_server_carries_only_the_tools_asked_for() -> None:
    """SEC-008, MCP-001: audience-bound and scoped per tool."""

    async def scenario(world: World) -> None:
        response = await world.stub.ExchangeToken(
            exchange(own_token(world), DOCUMENTATION, (SEARCH, SEARCH)),
            metadata=as_user(),
            timeout=5,
        )
        assert (response.audience, list(response.scopes)) == (DOCUMENTATION, [SEARCH])
        sent = world.keycloak.exchanges[0]
        assert (sent["audience"], sent["scope"]) == (
            DOCUMENTATION,
            f"{DOCUMENTATION}-audience {SEARCH}",
        )

    with_service(scenario)


@pytest.mark.parametrize(
    ("audience", "scopes"),
    [
        (DOCUMENTATION, ()),
        (DOCUMENTATION, ("mcp.tavily.search_web",)),
        (DOCUMENTATION, (SEARCH, "mcp.documentation.delete_documents")),
        (DOCUMENTATION, (f"{DOCUMENTATION}-audience",)),
        (AGENT, (SEARCH,)),
    ],
)
def test_a_token_cannot_carry_a_scope_its_service_does_not_have(
    audience: str, scopes: tuple[str, ...]
) -> None:
    async def scenario(world: World) -> None:
        call = world.stub.ExchangeToken(
            exchange(own_token(world), audience, scopes), metadata=as_user(), timeout=5
        )
        assert await code_of(call) is grpc.StatusCode.INVALID_ARGUMENT
        assert world.keycloak.exchanges == []

    with_service(scenario)


def test_a_service_identity_cannot_exchange_a_token() -> None:
    async def scenario(world: World) -> None:
        token = sign_identity(VerifiedIdentity(subject="svc-x", tenant_id=TENANT), SECRET)
        call = world.stub.ExchangeToken(
            exchange(own_token(world)), metadata=(("x-windops-identity", token),), timeout=5
        )
        assert await code_of(call) is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_keycloaks_refusal_is_passed_on() -> None:
    async def scenario(world: World) -> None:
        world.keycloak.refuse_exchange = True
        call = world.stub.ExchangeToken(exchange(own_token(world)), metadata=as_user(), timeout=5)
        assert await code_of(call) is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


# --- Agent identities (M8.5, ADR 0019)

CASE = "case_" + "a" * 20
DOCUMENTATION = "windops-mcp-documentation"
SEARCH = "mcp.documentation.search_documents"


def as_investigation(tenant: str = TENANT) -> Any:
    own = VerifiedIdentity(subject="service:investigation", tenant_id=tenant)
    return (("x-windops-identity", sign_identity(own, SECRET)),)


def start(token: str = "", key: str = "start-key-1", ttl: int = 0) -> Any:
    return identity_pb2.StartDelegationRequest(
        case_id=CASE, subject_token=token, idempotency_key=key, ttl_seconds=ttl
    )


def issue(delegation: str, agent: str = "reliability-engineer", audience: str = DOCUMENTATION,
          scopes: tuple[str, ...] = (SEARCH,)) -> Any:  # fmt: skip
    return identity_pb2.IssueAgentTokenRequest(
        delegation_id=delegation, agent=agent, audience=audience, scopes=list(scopes)
    )


def claims_of(world: World, token: str) -> dict[str, Any]:
    import jwt

    public = jwt.PyJWK(world.jwks["keys"][0]).key
    decoded: dict[str, Any] = jwt.decode(
        token, public, algorithms=["ES256"], audience=DOCUMENTATION, issuer=AGENT_ISSUER
    )
    return decoded


def test_a_user_delegates_and_an_agent_gets_a_short_token_naming_both() -> None:
    """SRS 9.2, SEC-008, SEC-010."""

    async def scenario(world: World) -> None:
        started = (
            await world.stub.StartDelegation(start(own_token(world)), metadata=as_user(), timeout=5)
        ).delegation
        assert (
            started.principal == f"user:{ADMIN}" and started.started_by == "windops-investigation"
        )
        assert started.expires_at.seconds - started.created_at.seconds == 3600
        again = (
            await world.stub.StartDelegation(start(own_token(world)), metadata=as_user(), timeout=5)
        ).delegation
        assert again.delegation_id == started.delegation_id
        world.peer[0] = "windops-agent-runtime"
        issued = await world.stub.IssueAgentToken(
            issue(started.delegation_id), metadata=as_investigation(), timeout=5
        )
        claims = claims_of(world, issued.access_token)
        assert claims["sub"] == ADMIN and claims["tenant_id"] == TENANT
        assert claims["act"] == {"sub": "agent:reliability-engineer"}
        assert claims["scope"] == SEARCH and claims["exp"] - claims["iat"] == 120
        assert (issued.actor, issued.principal) == ("agent:reliability-engineer", f"user:{ADMIN}")
        assert (TENANT, "identity.delegation.started") in await audit_actions(world.engine)

    with_service(scenario)


def test_the_fault_investigator_is_only_for_the_investigation_service_as_itself() -> None:
    async def scenario(world: World) -> None:
        started = (
            await world.stub.StartDelegation(start(), metadata=as_investigation(), timeout=5)
        ).delegation
        assert started.principal == "service:fault-investigator"
        world.peer[0] = "windops-agent-runtime"
        issued = await world.stub.IssueAgentToken(
            issue(started.delegation_id, agent="safety-researcher"), metadata=as_investigation(),
            timeout=5,
        )  # fmt: skip
        assert claims_of(world, issued.access_token)["sub"] == "service:fault-investigator"
        # Not for a user, nor through Web BFF.
        world.peer[0] = "windops-investigation"
        assert (
            await code_of(
                world.stub.StartDelegation(start(key="start-key-2"), metadata=as_user(), timeout=5)
            )
            is grpc.StatusCode.PERMISSION_DENIED
        )
        world.peer[0] = "windops-web-bff"
        assert (
            await code_of(
                world.stub.StartDelegation(
                    start(key="start-key-3"), metadata=as_investigation(), timeout=5
                )
            )
            is grpc.StatusCode.PERMISSION_DENIED
        )

    with_service(scenario)


def test_each_call_is_open_only_to_the_services_that_should_make_it() -> None:
    async def scenario(world: World) -> None:
        delegation = (
            await world.stub.StartDelegation(start(), metadata=as_investigation(), timeout=5)
        ).delegation.delegation_id
        for peer in ("windops-agent-runtime", "windops-check-client", ""):
            world.peer[0] = peer
            assert await code_of(world.stub.StartDelegation(
                start(key="start-key-9"), metadata=as_investigation(), timeout=5)
            ) is grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        for peer in ("windops-investigation", "windops-web-bff", "windops-check-client"):
            world.peer[0] = peer
            assert await code_of(world.stub.IssueAgentToken(
                issue(delegation), metadata=as_investigation(), timeout=5)
            ) is grpc.StatusCode.PERMISSION_DENIED  # fmt: skip

    with_service(scenario)


@pytest.mark.parametrize(
    ("request_change", "code"),
    [
        ({"agent": "approver"}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"audience": "windops-agent-runtime"}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"scopes": ("mcp.weather.get_forecast",)}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"scopes": ()}, grpc.StatusCode.INVALID_ARGUMENT),
    ],
)
def test_a_token_is_only_for_a_known_agent_and_one_servers_own_tools(
    request_change: dict[str, Any], code: grpc.StatusCode
) -> None:
    async def scenario(world: World) -> None:
        delegation = (
            await world.stub.StartDelegation(start(), metadata=as_investigation(), timeout=5)
        ).delegation.delegation_id
        world.peer[0] = "windops-agent-runtime"
        call = world.stub.IssueAgentToken(
            issue(delegation, **request_change), metadata=as_investigation(), timeout=5
        )
        assert await code_of(call) is code

    with_service(scenario)


def test_no_token_once_ended_for_another_tenant_or_for_a_disabled_user() -> None:
    async def scenario(world: World) -> None:
        ended = (
            await world.stub.StartDelegation(start(), metadata=as_investigation(), timeout=5)
        ).delegation.delegation_id
        users = (
            await world.stub.StartDelegation(
                start(own_token(world), key="start-key-2"), metadata=as_user(), timeout=5
            )
        ).delegation.delegation_id
        world.peer[0] = "windops-agent-runtime"
        stop = identity_pb2.EndDelegationRequest(delegation_id=ended, reason="Run completed")
        first = await world.stub.EndDelegation(stop, metadata=as_investigation(), timeout=5)
        twice = await world.stub.EndDelegation(stop, metadata=as_investigation(), timeout=5)
        assert first.delegation.ended_at == twice.delegation.ended_at
        assert await code_of(world.stub.IssueAgentToken(
            issue(ended), metadata=as_investigation(), timeout=5)
        ) is grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        assert await code_of(world.stub.IssueAgentToken(
            issue(users), metadata=as_investigation(OTHER), timeout=5)
        ) is grpc.StatusCode.NOT_FOUND  # fmt: skip
        world.keycloak.users[ADMIN]["enabled"] = False
        assert await code_of(world.stub.IssueAgentToken(
            issue(users), metadata=as_investigation(), timeout=5)
        ) is grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        actions = await audit_actions(world.engine)
        assert actions.count((TENANT, "identity.delegation.ended")) == 1

    with_service(scenario)


@pytest.mark.parametrize("ttl", [30, 4 * 3600 + 1])
def test_a_delegation_lasts_a_minute_to_four_hours(ttl: int) -> None:
    async def scenario(world: World) -> None:
        call = world.stub.StartDelegation(start(ttl=ttl), metadata=as_investigation(), timeout=5)
        assert await code_of(call) is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_only_a_users_own_current_token_starts_a_delegation_for_them() -> None:
    async def scenario(world: World) -> None:
        world.keycloak.tokens["stolen"] = {"active": True, "sub": "someone", "tenant_id": TENANT}
        call = world.stub.StartDelegation(start("stolen"), metadata=as_user(), timeout=5)
        assert await code_of(call) is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_the_published_keys_are_public_only() -> None:
    async def scenario(world: World) -> None:
        world.peer[0] = "windops-mcp-documentation"
        answer = await world.stub.GetAgentTokenKeys(
            identity_pb2.GetAgentTokenKeysRequest(), metadata=as_investigation(), timeout=5
        )
        keys = json.loads(answer.jwks_json)["keys"]
        assert answer.issuer == AGENT_ISSUER and len(keys) == 1
        assert keys[0]["alg"] == "ES256" and "d" not in keys[0]

    with_service(scenario)


@pytest.mark.parametrize(
    ("agent", "audience", "scope"),
    [
        ("maintenance-planner", "windops-mcp-tavily", "mcp.tavily.search_web"),
        ("scada-analyst", DOCUMENTATION, SEARCH),
        ("safety-researcher", "windops-mcp-windops", "mcp.windops.check_policy"),
        ("supervisor", "windops-mcp-windops", "mcp.windops.estimate_impact"),
    ],
)
def test_an_agent_gets_no_token_for_a_tool_outside_its_allowlist(
    agent: str, audience: str, scope: str
) -> None:
    """MCP-002, MCP-007: fixed in code, so nothing an agent reads can widen it."""

    async def scenario(world: World) -> None:
        delegation = (
            await world.stub.StartDelegation(start(), metadata=as_investigation(), timeout=5)
        ).delegation.delegation_id
        world.peer[0] = "windops-agent-runtime"
        call = world.stub.IssueAgentToken(
            issue(delegation, agent=agent, audience=audience, scopes=(scope,)),
            metadata=as_investigation(), timeout=5,
        )  # fmt: skip
        assert await code_of(call) is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)
