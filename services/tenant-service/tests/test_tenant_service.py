"""The API through a real gRPC server and SQL database, with stand-ins for the other services.

SQLite has no row-level security, so `make tenant-check` proves that part against PostgreSQL.
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
from windops.tenant.v1 import tenant_pb2, tenant_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_events import parse_event
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, sign_identity
from windops_outbox import OUTBOX_TABLE, create_outbox_table
from windops_tenant.service import TenantService, add_tenant
from windops_tenant.store import METADATA, SERVICE_NAME

SECRET = "x" * 32
TENANT, OTHER, PLATFORM = "north-sea", "atlas-wind", "windops-platform"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
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
    """Grants by (user, permission). The Authorization Service has its own tests."""

    grants: set[tuple[str, str]] = field(default_factory=set)
    asked: list[tuple[str, str, str]] = field(default_factory=list)

    async def allowed(self, permission: str, target_tenant_id: str) -> bool:
        from windops_grpc import current_identity

        identity = current_identity()
        user = identity.user_id or ""
        self.asked.append((user, permission, target_tenant_id))
        # As the real service decides: only platform staff may name another tenant.
        if target_tenant_id and identity.tenant_id != PLATFORM:
            return False
        return (user, permission) in self.grants


@dataclass
class FakeDirectory:
    created: dict[str, str] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=dict)

    async def create(self, tenant_id: str, name: str) -> None:
        self.created[tenant_id] = name
        self.enabled[tenant_id] = True

    async def set_enabled(self, tenant_id: str, enabled: bool) -> None:
        self.enabled[tenant_id] = enabled


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    authorizer: FakeAuthorizer
    directory: FakeDirectory


def as_user(user: str = "u-admin", tenant: str = TENANT, *, level: str | None = LEVEL_MFA) -> Any:
    identity = VerifiedIdentity(
        subject=user,
        tenant_id=tenant,
        user_id=user,
        auth_level=level,
        auth_time=int(time.time()) if level else None,
    )
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


def as_service(tenant: str = TENANT) -> Any:
    """A service acting for a tenant, such as Web BFF before anyone signs in."""
    token = sign_identity(VerifiedIdentity(subject="svc-web-bff", tenant_id=tenant), SECRET)
    return (("x-windops-identity", token),)


PLATFORM_ADMIN = as_user("u-platform", PLATFORM)


async def audit_actions(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(select(OUTBOX_TABLE.c.subject, OUTBOX_TABLE.c.envelope))
        ).all()
    return [
        (event.tenant_id, event.payload.action)  # type: ignore[attr-defined]
        for event in (parse_event(row.subject, row.envelope) for row in rows)
    ]


def with_service(scenario: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        from datetime import UTC, datetime

        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
            for tenant, name in (
                (TENANT, "North Sea Wind"),
                (OTHER, "Atlas Wind"),
                (PLATFORM, "Platform"),
            ):
                await add_tenant(connection, tenant, name, actor="test", now=datetime.now(UTC))
        authorizer = FakeAuthorizer(
            {
                ("u-admin", "branding.manage"),
                ("u-platform", "tenant.manage"),
                ("u-atlas-admin", "branding.manage"),
            }
        )
        directory = FakeDirectory()

        def register(server: grpc.aio.Server) -> None:
            tenant_pb2_grpc.add_TenantServiceServicer_to_server(
                TenantService(engine, authorizer, directory), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub: Any = tenant_pb2_grpc.TenantServiceStub(channel)
                await scenario(World(stub, engine, authorizer, directory))
        finally:
            await runtime.stop()
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


def branding(**changes: str) -> Any:
    values = {
        "display_name": "North Sea Wind",
        "primary_color": "#0b5394",
        "accent_color": "#38761d",
        "login_title": "Sign in",
        "login_message": "Authorized staff only",
        "time_zone": "Europe/Oslo",
        "locale": "en-GB",
        **changes,
    }
    return tenant_pb2.UpdateBrandingRequest(**values)


def test_anyone_in_a_tenant_reads_its_branding_even_a_service() -> None:
    async def scenario(world: World) -> None:
        for metadata in (as_user("u-operator"), as_service()):
            response = await world.stub.GetBranding(
                tenant_pb2.GetBrandingRequest(), metadata=metadata, timeout=5
            )
            assert response.branding.tenant_id == TENANT
        assert world.authorizer.asked == []

    with_service(scenario)


def test_another_tenants_branding_needs_tenant_manage() -> None:
    """SEC-007, and the platform's own staff can read it."""

    async def scenario(world: World) -> None:
        other = tenant_pb2.GetBrandingRequest(target_tenant_id=OTHER)
        for method in ("GetBranding", "GetTenant", "GetLogo", "ListEmailTemplates"):
            request_type = getattr(tenant_pb2, f"{method}Request")
            code = await code_of(
                getattr(world.stub, method)(
                    request_type(target_tenant_id=OTHER), metadata=as_user(), timeout=5
                )
            )
            assert code is grpc.StatusCode.PERMISSION_DENIED, method
        allowed = await world.stub.GetBranding(other, metadata=PLATFORM_ADMIN, timeout=5)
        assert allowed.branding.tenant_id == OTHER

    with_service(scenario)


def test_an_administrator_updates_branding_with_a_recent_second_factor() -> None:
    async def scenario(world: World) -> None:
        code = await code_of(
            world.stub.UpdateBranding(branding(), metadata=as_user(level=LEVEL_PASSWORD), timeout=5)
        )
        assert code is grpc.StatusCode.PERMISSION_DENIED
        updated = await world.stub.UpdateBranding(branding(), metadata=as_user(), timeout=5)
        assert updated.branding.time_zone == "Europe/Oslo"
        assert (TENANT, "tenant.branding.updated") in await audit_actions(world.engine)

    with_service(scenario)


def test_a_user_without_branding_manage_cannot_change_it() -> None:
    async def scenario(world: World) -> None:
        code = await code_of(
            world.stub.UpdateBranding(branding(), metadata=as_user("u-operator"), timeout=5)
        )
        assert code is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_a_tenant_administrator_cannot_change_another_tenant() -> None:
    async def scenario(world: World) -> None:
        request = branding(display_name="Taken over")
        request.target_tenant_id = OTHER
        code = await code_of(world.stub.UpdateBranding(request, metadata=as_user(), timeout=5))
        assert code is grpc.StatusCode.PERMISSION_DENIED
        other = await world.stub.GetBranding(
            tenant_pb2.GetBrandingRequest(), metadata=as_service(OTHER), timeout=5
        )
        assert other.branding.display_name == "Atlas Wind"

    with_service(scenario)


def test_unreadable_branding_is_refused() -> None:
    async def scenario(world: World) -> None:
        code = await code_of(
            world.stub.UpdateBranding(
                branding(primary_color="#ffff99"), metadata=as_user(), timeout=5
            )
        )
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_a_logo_is_stored_and_served_as_set() -> None:
    async def scenario(world: World) -> None:
        set_logo = await world.stub.SetLogo(
            tenant_pb2.SetLogoRequest(image=PNG, content_type="image/png"),
            metadata=as_user(),
            timeout=5,
        )
        assert set_logo.branding.has_logo
        logo = await world.stub.GetLogo(
            tenant_pb2.GetLogoRequest(), metadata=as_service(), timeout=5
        )
        assert (logo.image, logo.content_type) == (PNG, "image/png")
        svg = tenant_pb2.SetLogoRequest(image=b"<svg/>", content_type="image/svg+xml")
        assert (
            await code_of(world.stub.SetLogo(svg, metadata=as_user(), timeout=5))
            is grpc.StatusCode.INVALID_ARGUMENT
        )

    with_service(scenario)


def test_email_templates_start_from_defaults_and_can_be_changed() -> None:
    async def scenario(world: World) -> None:
        listed = await world.stub.ListEmailTemplates(
            tenant_pb2.ListEmailTemplatesRequest(), metadata=as_service(), timeout=5
        )
        assert {t.template for t in listed.templates} == {
            "invitation",
            "password_reset",
            "approval_request",
        }
        assert not any(t.customized for t in listed.templates)
        await world.stub.SetEmailTemplate(
            tenant_pb2.SetEmailTemplateRequest(
                template="invitation",
                subject="Join {{tenant_name}}",
                body="Hi {{user_name}}: {{login_url}}",
            ),
            metadata=as_user(),
            timeout=5,
        )
        listed = await world.stub.ListEmailTemplates(
            tenant_pb2.ListEmailTemplatesRequest(), metadata=as_service(), timeout=5
        )
        invitation = next(t for t in listed.templates if t.template == "invitation")
        assert invitation.customized and invitation.subject == "Join {{tenant_name}}"
        other = await world.stub.ListEmailTemplates(
            tenant_pb2.ListEmailTemplatesRequest(), metadata=as_service(OTHER), timeout=5
        )
        assert not any(t.customized for t in other.templates)

    with_service(scenario)


def test_a_domain_belongs_to_one_tenant_and_the_refusal_names_nobody() -> None:
    async def scenario(world: World) -> None:
        mine = await world.stub.SetCustomDomain(
            tenant_pb2.SetCustomDomainRequest(domain="ops.north-sea.example"),
            metadata=as_user(),
            timeout=5,
        )
        assert mine.custom_domain.status == "pending"
        assert mine.custom_domain.verification_token.startswith("windops-verify-")
        again = await world.stub.SetCustomDomain(
            tenant_pb2.SetCustomDomainRequest(domain="ops.north-sea.example"),
            metadata=as_user(),
            timeout=5,
        )
        assert again.custom_domain.verification_token == mine.custom_domain.verification_token
        taken = tenant_pb2.SetCustomDomainRequest(domain="ops.north-sea.example")
        code = await code_of(
            world.stub.SetCustomDomain(taken, metadata=as_user("u-atlas-admin", OTHER), timeout=5)
        )
        assert code is grpc.StatusCode.FAILED_PRECONDITION
        await world.stub.RemoveCustomDomain(
            tenant_pb2.RemoveCustomDomainRequest(), metadata=as_user(), timeout=5
        )
        shown = await world.stub.GetBranding(
            tenant_pb2.GetBrandingRequest(), metadata=as_service(), timeout=5
        )
        assert not shown.HasField("custom_domain")

    with_service(scenario)


def test_platform_staff_create_a_tenant_with_its_organization() -> None:
    async def scenario(world: World) -> None:
        request = tenant_pb2.CreateTenantRequest(target_tenant_id="gale-force", name="Gale Force")
        created = await world.stub.CreateTenant(request, metadata=PLATFORM_ADMIN, timeout=5)
        assert created.created and created.tenant.status == "active"
        assert world.directory.created == {"gale-force": "Gale Force"}
        again = await world.stub.CreateTenant(request, metadata=PLATFORM_ADMIN, timeout=5)
        assert not again.created
        clash = tenant_pb2.CreateTenantRequest(target_tenant_id="gale-force", name="Other")
        assert (
            await code_of(world.stub.CreateTenant(clash, metadata=PLATFORM_ADMIN, timeout=5))
            is grpc.StatusCode.FAILED_PRECONDITION
        )
        listed = await world.stub.ListTenants(
            tenant_pb2.ListTenantsRequest(), metadata=PLATFORM_ADMIN, timeout=5
        )
        assert "gale-force" in {t.tenant_id for t in listed.tenants}

    with_service(scenario)


def test_a_tenant_administrator_cannot_create_list_or_suspend_tenants() -> None:
    async def scenario(world: World) -> None:
        admin = as_user()
        for call in (
            world.stub.CreateTenant(
                tenant_pb2.CreateTenantRequest(target_tenant_id="gale", name="Gale"),
                metadata=admin,
                timeout=5,
            ),
            world.stub.ListTenants(tenant_pb2.ListTenantsRequest(), metadata=admin, timeout=5),
            world.stub.SuspendTenant(
                tenant_pb2.SuspendTenantRequest(
                    target_tenant_id=OTHER, reason="Unpaid invoices for months"
                ),
                metadata=admin,
                timeout=5,
            ),
        ):
            assert await code_of(call) is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_suspending_a_tenant_disables_its_organization_and_is_audited() -> None:
    async def scenario(world: World) -> None:
        suspended = await world.stub.SuspendTenant(
            tenant_pb2.SuspendTenantRequest(
                target_tenant_id=OTHER, reason="Contract ended this month"
            ),
            metadata=PLATFORM_ADMIN,
            timeout=5,
        )
        assert suspended.tenant.status == "suspended"
        assert world.directory.enabled[OTHER] is False
        await world.stub.ReactivateTenant(
            tenant_pb2.ReactivateTenantRequest(target_tenant_id=OTHER),
            metadata=PLATFORM_ADMIN,
            timeout=5,
        )
        assert world.directory.enabled[OTHER] is True
        actions = await audit_actions(world.engine)
        assert {(OTHER, "tenant.suspended"), (OTHER, "tenant.reactivated")} <= set(actions)

    with_service(scenario)


@pytest.mark.parametrize("target", [PLATFORM, "../x"])
def test_the_platform_tenant_cannot_be_suspended_and_ids_are_checked(target: str) -> None:
    async def scenario(world: World) -> None:
        request = tenant_pb2.SuspendTenantRequest(
            target_tenant_id=target, reason="Testing the refusal"
        )
        code = await code_of(world.stub.SuspendTenant(request, metadata=PLATFORM_ADMIN, timeout=5))
        assert code is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)
