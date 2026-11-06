"""The gRPC API. Each transaction names its tenant first, so row-level security applies to it."""

import logging
import secrets
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.authorization.v1 import authorization_pb2
from windops.identity.v1 import identity_pb2
from windops.tenant.v1 import tenant_pb2, tenant_pb2_grpc
from windops_auth import (
    STEP_UP_MESSAGE,
    MfaRequiredError,
    authentication_claims,
    require_recent_mfa,
)
from windops_grpc import (
    ConflictError,
    DomainError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ServiceClient,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
)
from windops_migrations import across_tenants, in_tenant
from windops_resilience import BreakerOpenError

from windops_tenant import branding as rules
from windops_tenant.branding import BrandingError, BrandingFields
from windops_tenant.store import BRANDING, CUSTOM_DOMAINS, EMAIL_TEMPLATES, TENANTS, audit

logger = logging.getLogger("windops.service")
STEP_UP = STEP_UP_MESSAGE
DEADLINE_SECONDS = 3.0
_DEFAULT_PAGE, _MAXIMUM_PAGE = 50, 200
_MAXIMUM_NAME = 200


class Authorizer(Protocol):
    async def allowed(self, permission: str, target_tenant_id: str) -> bool: ...


class Directory(Protocol):
    """The tenant's Keycloak organization, which the Identity Service manages."""

    async def create(self, tenant_id: str, name: str) -> None: ...
    async def set_enabled(self, tenant_id: str, enabled: bool) -> None: ...


def _unavailable(error: Exception, what: str) -> DomainError:
    """Pass a refusal on as it is, and turn anything else into UNAVAILABLE (SRS 7.7)."""
    if isinstance(error, grpc.aio.AioRpcError):
        passed_on = {
            grpc.StatusCode.PERMISSION_DENIED: PermissionDeniedError,
            grpc.StatusCode.NOT_FOUND: NotFoundError,
            grpc.StatusCode.FAILED_PRECONDITION: ConflictError,
            grpc.StatusCode.INVALID_ARGUMENT: InvalidRequestError,
        }
        kind = passed_on.get(error.code())
        if kind is not None:
            return kind(error.details() or "Refused")
    return UnavailableError(f"{what} is unavailable right now")


class AuthorizationClient:
    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, target_tenant_id: str) -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission, target_tenant_id=target_tenant_id
        )
        try:
            response = await self._client.call(
                self._stub.Check, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(response.allowed)


class IdentityDirectory:
    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def create(self, tenant_id: str, name: str) -> None:
        request = identity_pb2.CreateOrganizationRequest(target_tenant_id=tenant_id, name=name)
        try:
            await self._client.call(
                self._stub.CreateOrganization,
                request,
                idempotent=True,
                deadline_seconds=DEADLINE_SECONDS,
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise _unavailable(error, "The Identity Service") from error

    async def set_enabled(self, tenant_id: str, enabled: bool) -> None:
        request = identity_pb2.SetOrganizationEnabledRequest(
            target_tenant_id=tenant_id, enabled=enabled
        )
        try:
            await self._client.call(
                self._stub.SetOrganizationEnabled,
                request,
                idempotent=True,
                deadline_seconds=DEADLINE_SECONDS,
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise _unavailable(error, "The Identity Service") from error


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime | None) -> Timestamp | None:
    if moment is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(moment if moment.tzinfo else moment.replace(tzinfo=UTC))
    return stamp


def _tenant(row: Any) -> tenant_pb2.Tenant:
    return tenant_pb2.Tenant(
        tenant_id=row.tenant_id,
        name=row.name,
        status=row.status,
        created_at=_timestamp(row.created_at),
    )


def _branding(row: Any) -> tenant_pb2.Branding:
    return tenant_pb2.Branding(
        tenant_id=row.tenant_id,
        display_name=row.display_name,
        primary_color=row.primary_color,
        accent_color=row.accent_color,
        login_title=row.login_title,
        login_message=row.login_message,
        time_zone=row.time_zone,
        locale=row.locale,
        has_logo=row.logo is not None,
        updated_at=_timestamp(row.updated_at),
    )


def _domain(row: Any) -> tenant_pb2.CustomDomain:
    return tenant_pb2.CustomDomain(
        domain=row.domain,
        status=row.status,
        verification_token=row.verification_token,
        created_at=_timestamp(row.created_at),
    )


async def add_tenant(
    connection: AsyncConnection, tenant_id: str, name: str, *, actor: str, now: datetime
) -> None:
    """Insert a tenant and its default branding, in a transaction already in that tenant."""
    defaults = rules.default_branding(name)
    await connection.execute(
        insert(TENANTS).values(
            tenant_id=tenant_id,
            name=name,
            status="active",
            created_by=actor,
            created_at=now,
            updated_at=now,
        )
    )
    await connection.execute(
        insert(BRANDING).values(
            tenant_id=tenant_id,
            **defaults.__dict__,
            updated_by=actor,
            updated_at=now,
        )
    )


class TenantService(tenant_pb2_grpc.TenantServiceServicer):
    def __init__(self, engine: AsyncEngine, authorizer: Authorizer, directory: Directory) -> None:
        self._engine = engine
        self._authorizer = authorizer
        self._directory = directory

    @staticmethod
    def _target(identity: VerifiedIdentity, target_tenant_id: str) -> str:
        tenant = target_tenant_id or identity.tenant_id
        try:
            rules.check_tenant_id(tenant)
        except BrandingError as error:
            raise InvalidRequestError(str(error)) from error
        return tenant

    async def _read(self, target_tenant_id: str) -> str:
        """Anyone in a tenant may read it. Reading another needs tenant.manage."""
        identity = current_identity()
        tenant = self._target(identity, target_tenant_id)
        if tenant != identity.tenant_id and not await self._authorizer.allowed(
            "tenant.manage", tenant
        ):
            raise PermissionDeniedError("You cannot read another tenant")
        return tenant

    async def _change(self, permission: str, target_tenant_id: str) -> tuple[str, str]:
        """Return the user and tenant for a change, or refuse."""
        identity = current_identity()
        tenant = self._target(identity, target_tenant_id)
        if not identity.user_id:
            raise PermissionDeniedError("Only a user can do this")
        target = "" if tenant == identity.tenant_id else tenant
        if not await self._authorizer.allowed(permission, target):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP) from error
        return identity.user_id, tenant

    @staticmethod
    async def _one(connection: AsyncConnection, query: Any) -> Any:
        row = (await connection.execute(query)).one_or_none()
        if row is None:
            raise NotFoundError("No such tenant")
        return row

    # --- the lifecycle, which only platform staff hold tenant.manage for

    async def CreateTenant(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.CreateTenantRequest, context: Any
    ) -> tenant_pb2.CreateTenantResponse:
        if not request.target_tenant_id:
            raise InvalidRequestError("Name the new tenant's ID")
        actor, tenant = await self._change("tenant.manage", request.target_tenant_id)
        name = request.name.strip()
        if not name or len(name) > _MAXIMUM_NAME:
            raise InvalidRequestError("A tenant name is required")
        if tenant == rules.PLATFORM_TENANT:
            raise InvalidRequestError("This tenant ID is reserved")
        # The organization first: a tenant without one could never sign anyone in.
        await self._directory.create(tenant, name)
        now = _now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            existing = (
                await connection.execute(select(TENANTS).where(TENANTS.c.tenant_id == tenant))
            ).one_or_none()
            if existing is not None:
                if existing.name != name:
                    raise ConflictError("A tenant with this ID already exists")
                return tenant_pb2.CreateTenantResponse(tenant=_tenant(existing), created=False)
            await add_tenant(connection, tenant, name, actor=actor, now=now)
            await audit(
                connection,
                tenant_id=tenant,
                action="tenant.created",
                actor_id=actor,
                resource_id=tenant,
            )
            row = await self._one(connection, select(TENANTS).where(TENANTS.c.tenant_id == tenant))
        return tenant_pb2.CreateTenantResponse(tenant=_tenant(row), created=True)

    async def GetTenant(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.GetTenantRequest, context: Any
    ) -> tenant_pb2.GetTenantResponse:
        tenant = await self._read(request.target_tenant_id)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._one(connection, select(TENANTS).where(TENANTS.c.tenant_id == tenant))
        return tenant_pb2.GetTenantResponse(tenant=_tenant(row))

    async def ListTenants(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.ListTenantsRequest, context: Any
    ) -> tenant_pb2.ListTenantsResponse:
        identity = current_identity()
        # Naming no tenant here would mean the caller's own, which proves nothing for platform work.
        if identity.tenant_id != rules.PLATFORM_TENANT or not await self._authorizer.allowed(
            "tenant.manage", ""
        ):
            raise PermissionDeniedError("Only platform staff can list tenants")
        size = request.page_size or _DEFAULT_PAGE
        if not 1 <= size <= _MAXIMUM_PAGE:
            raise InvalidRequestError(f"The page size must be from 1 to {_MAXIMUM_PAGE}")
        query = select(TENANTS).order_by(TENANTS.c.tenant_id).limit(size + 1)
        if request.page_token:
            query = query.where(TENANTS.c.tenant_id > request.page_token)
        async with self._engine.begin() as connection:
            # The one place that reads every tenant, on purpose.
            await across_tenants(connection)
            rows = (await connection.execute(query)).all()
        following = rows[size - 1].tenant_id if len(rows) > size else ""
        return tenant_pb2.ListTenantsResponse(
            tenants=[_tenant(row) for row in rows[:size]], next_page_token=following
        )

    async def _set_status(self, target_tenant_id: str, *, active: bool, reason: str) -> Any:
        if not target_tenant_id:
            raise InvalidRequestError("Name the tenant")
        actor, tenant = await self._change("tenant.manage", target_tenant_id)
        if tenant == rules.PLATFORM_TENANT:
            raise InvalidRequestError("The platform tenant cannot be suspended")
        await self._directory.set_enabled(tenant, active)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._one(connection, select(TENANTS).where(TENANTS.c.tenant_id == tenant))
            status = "active" if active else "suspended"
            if row.status != status:
                await connection.execute(
                    update(TENANTS)
                    .where(TENANTS.c.tenant_id == tenant)
                    .values(
                        status=status,
                        suspended_reason=None if active else reason,
                        updated_at=_now(),
                    )
                )
                await audit(
                    connection,
                    tenant_id=tenant,
                    action=f"tenant.{'reactivated' if active else 'suspended'}",
                    actor_id=actor,
                    resource_id=tenant,
                )
            return await self._one(connection, select(TENANTS).where(TENANTS.c.tenant_id == tenant))

    async def SuspendTenant(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.SuspendTenantRequest, context: Any
    ) -> tenant_pb2.SuspendTenantResponse:
        try:
            reason = rules.check_reason(request.reason)
        except BrandingError as error:
            raise InvalidRequestError(str(error)) from error
        row = await self._set_status(request.target_tenant_id, active=False, reason=reason)
        return tenant_pb2.SuspendTenantResponse(tenant=_tenant(row))

    async def ReactivateTenant(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.ReactivateTenantRequest, context: Any
    ) -> tenant_pb2.ReactivateTenantResponse:
        row = await self._set_status(request.target_tenant_id, active=True, reason="")
        return tenant_pb2.ReactivateTenantResponse(tenant=_tenant(row))

    # --- branding, which each tenant's administrators hold branding.manage for

    async def GetBranding(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.GetBrandingRequest, context: Any
    ) -> tenant_pb2.GetBrandingResponse:
        tenant = await self._read(request.target_tenant_id)
        if request.include_verification_token:
            # Only those who manage the domain see its proof. Asked only when wanted, as each
            # refusal is recorded as an access denial.
            identity = current_identity()
            target = "" if tenant == identity.tenant_id else tenant
            if not identity.user_id or not await self._authorizer.allowed(
                "branding.manage", target
            ):
                raise PermissionDeniedError("You do not have permission to do this")
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._one(
                connection, select(BRANDING).where(BRANDING.c.tenant_id == tenant)
            )
            domain = (
                await connection.execute(
                    select(CUSTOM_DOMAINS).where(CUSTOM_DOMAINS.c.tenant_id == tenant)
                )
            ).one_or_none()
        shown = _domain(domain) if domain else None
        if shown is not None and not request.include_verification_token:
            shown.verification_token = ""
        return tenant_pb2.GetBrandingResponse(branding=_branding(row), custom_domain=shown)

    async def UpdateBranding(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.UpdateBrandingRequest, context: Any
    ) -> tenant_pb2.UpdateBrandingResponse:
        actor, tenant = await self._change("branding.manage", request.target_tenant_id)
        try:
            fields = rules.check_branding(
                BrandingFields(
                    display_name=request.display_name,
                    primary_color=request.primary_color,
                    accent_color=request.accent_color,
                    login_title=request.login_title,
                    login_message=request.login_message,
                    time_zone=request.time_zone,
                    locale=request.locale,
                )
            )
        except BrandingError as error:
            raise InvalidRequestError(str(error)) from error
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await self._one(
                connection, select(BRANDING.c.tenant_id).where(BRANDING.c.tenant_id == tenant)
            )
            await connection.execute(
                update(BRANDING)
                .where(BRANDING.c.tenant_id == tenant)
                .values(**fields.__dict__, updated_by=actor, updated_at=_now())
            )
            await audit(
                connection,
                tenant_id=tenant,
                action="tenant.branding.updated",
                actor_id=actor,
                resource_id=tenant,
            )
            row = await self._one(
                connection, select(BRANDING).where(BRANDING.c.tenant_id == tenant)
            )
        return tenant_pb2.UpdateBrandingResponse(branding=_branding(row))

    async def SetLogo(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.SetLogoRequest, context: Any
    ) -> tenant_pb2.SetLogoResponse:
        actor, tenant = await self._change("branding.manage", request.target_tenant_id)
        try:
            rules.check_logo(request.image, request.content_type)
        except BrandingError as error:
            raise InvalidRequestError(str(error)) from error
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await self._one(
                connection, select(BRANDING.c.tenant_id).where(BRANDING.c.tenant_id == tenant)
            )
            await connection.execute(
                update(BRANDING)
                .where(BRANDING.c.tenant_id == tenant)
                .values(
                    logo=request.image,
                    logo_type=request.content_type,
                    updated_by=actor,
                    updated_at=_now(),
                )
            )
            await audit(
                connection,
                tenant_id=tenant,
                action="tenant.logo.updated",
                actor_id=actor,
                resource_id=tenant,
            )
            row = await self._one(
                connection, select(BRANDING).where(BRANDING.c.tenant_id == tenant)
            )
        return tenant_pb2.SetLogoResponse(branding=_branding(row))

    async def GetLogo(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.GetLogoRequest, context: Any
    ) -> tenant_pb2.GetLogoResponse:
        tenant = await self._read(request.target_tenant_id)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._one(
                connection,
                select(BRANDING.c.logo, BRANDING.c.logo_type).where(BRANDING.c.tenant_id == tenant),
            )
        if row.logo is None:
            raise NotFoundError("The tenant has no logo")
        return tenant_pb2.GetLogoResponse(image=row.logo, content_type=row.logo_type)

    async def SetEmailTemplate(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.SetEmailTemplateRequest, context: Any
    ) -> tenant_pb2.SetEmailTemplateResponse:
        actor, tenant = await self._change("branding.manage", request.target_tenant_id)
        try:
            rules.check_template(request.template, request.subject, request.body)
        except BrandingError as error:
            raise InvalidRequestError(str(error)) from error
        now = _now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await self._one(
                connection, select(TENANTS.c.tenant_id).where(TENANTS.c.tenant_id == tenant)
            )
            await connection.execute(
                delete(EMAIL_TEMPLATES).where(
                    EMAIL_TEMPLATES.c.tenant_id == tenant,
                    EMAIL_TEMPLATES.c.template == request.template,
                )
            )
            await connection.execute(
                insert(EMAIL_TEMPLATES).values(
                    tenant_id=tenant,
                    template=request.template,
                    subject=request.subject,
                    body=request.body,
                    updated_by=actor,
                    updated_at=now,
                )
            )
            await audit(
                connection,
                tenant_id=tenant,
                action=f"tenant.email_template.updated.{request.template}",
                actor_id=actor,
                resource_id=tenant,
            )
        return tenant_pb2.SetEmailTemplateResponse(
            template=tenant_pb2.EmailTemplate(
                template=request.template,
                subject=request.subject,
                body=request.body,
                customized=True,
                placeholders=sorted(rules.TEMPLATES[request.template].placeholders),
            )
        )

    async def ListEmailTemplates(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.ListEmailTemplatesRequest, context: Any
    ) -> tenant_pb2.ListEmailTemplatesResponse:
        tenant = await self._read(request.target_tenant_id)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await self._one(
                connection, select(TENANTS.c.tenant_id).where(TENANTS.c.tenant_id == tenant)
            )
            rows = (
                await connection.execute(
                    select(EMAIL_TEMPLATES).where(EMAIL_TEMPLATES.c.tenant_id == tenant)
                )
            ).all()
        custom = {row.template: row for row in rows}
        templates = []
        for name, default in sorted(rules.TEMPLATES.items()):
            row = custom.get(name)
            templates.append(
                tenant_pb2.EmailTemplate(
                    template=name,
                    subject=row.subject if row else default.subject,
                    body=row.body if row else default.body,
                    customized=row is not None,
                    placeholders=sorted(default.placeholders),
                )
            )
        return tenant_pb2.ListEmailTemplatesResponse(templates=templates)

    async def SetCustomDomain(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.SetCustomDomainRequest, context: Any
    ) -> tenant_pb2.SetCustomDomainResponse:
        actor, tenant = await self._change("branding.manage", request.target_tenant_id)
        try:
            domain = rules.check_domain(request.domain)
        except BrandingError as error:
            raise InvalidRequestError(str(error)) from error
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                current = (
                    await connection.execute(
                        select(CUSTOM_DOMAINS).where(CUSTOM_DOMAINS.c.tenant_id == tenant)
                    )
                ).one_or_none()
                if current is not None and current.domain == domain:
                    return tenant_pb2.SetCustomDomainResponse(custom_domain=_domain(current))
                await connection.execute(
                    delete(CUSTOM_DOMAINS).where(CUSTOM_DOMAINS.c.tenant_id == tenant)
                )
                await connection.execute(
                    insert(CUSTOM_DOMAINS).values(
                        tenant_id=tenant,
                        domain=domain,
                        status="pending",
                        verification_token=f"windops-verify-{secrets.token_urlsafe(24)}",
                        created_at=_now(),
                    )
                )
                await audit(
                    connection,
                    tenant_id=tenant,
                    action="tenant.custom_domain.set",
                    actor_id=actor,
                    resource_id=domain,
                )
                row = await self._one(
                    connection, select(CUSTOM_DOMAINS).where(CUSTOM_DOMAINS.c.tenant_id == tenant)
                )
        except IntegrityError as error:
            # Another tenant holds it. The same answer as for a reserved domain, so it names nobody.
            raise ConflictError("This domain cannot be used") from error
        return tenant_pb2.SetCustomDomainResponse(custom_domain=_domain(row))

    async def RemoveCustomDomain(  # noqa: N802  # The name comes from the contract.
        self, request: tenant_pb2.RemoveCustomDomainRequest, context: Any
    ) -> tenant_pb2.RemoveCustomDomainResponse:
        actor, tenant = await self._change("branding.manage", request.target_tenant_id)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            removed = await connection.execute(
                delete(CUSTOM_DOMAINS).where(CUSTOM_DOMAINS.c.tenant_id == tenant)
            )
            if removed.rowcount:
                await audit(
                    connection,
                    tenant_id=tenant,
                    action="tenant.custom_domain.removed",
                    actor_id=actor,
                    resource_id=tenant,
                )
        return tenant_pb2.RemoveCustomDomainResponse()
