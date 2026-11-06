"""The Platform console's API, for the Platform Administrator across tenants (M3.10, RBAC-003).

It decides nothing. The Tenant Service needs tenant.manage, which only a Platform Administrator
holds, and Authorization allows break-glass only to one. Every change also needs a recent second
factor. A tenant's branding is read-only here: its own administrators change it (SRS 3.2).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from windops.authorization.v1 import authorization_pb2
from windops.tenant.v1 import tenant_pb2

from windops_web_bff.common import ID_PATTERN, TENANT_PATTERN, Call, Key, TenantId, time_of
from windops_web_bff.tokens import Caller


class Tenant(BaseModel):
    tenant_id: str
    name: str
    # "active" or "suspended".
    status: str
    created_at: str | None


class TenantPage(BaseModel):
    tenants: list[Tenant]
    next_page_token: str


class NewTenant(BaseModel):
    tenant_id: str = Field(pattern=TENANT_PATTERN)
    name: str = Field(min_length=1, max_length=200)


class CreatedTenant(BaseModel):
    tenant: Tenant
    # False when it already existed with this name.
    created: bool


class Suspension(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


class DomainStatus(BaseModel):
    domain: str
    # "pending" until it is proven.
    status: str


class TenantBranding(BaseModel):
    """What a tenant shows its users. The domain's proof token is the tenant's, so it stays out."""

    tenant_id: str
    display_name: str
    primary_color: str
    accent_color: str
    login_title: str
    login_message: str
    time_zone: str
    locale: str
    has_logo: bool
    updated_at: str | None
    custom_domain: DomainStatus | None


class BreakGlassGrant(BaseModel):
    grant_id: str
    tenant_id: str
    reason: str
    started_at: str | None
    expires_at: str | None
    ended_at: str | None


class BreakGlassGrants(BaseModel):
    grants: list[BreakGlassGrant]


class NewBreakGlass(BaseModel):
    tenant_id: str = Field(pattern=TENANT_PATTERN)
    # At least 20 characters, kept in the tenant's own audit record.
    reason: str = Field(min_length=20, max_length=500)
    # From 1 to 240.
    duration_minutes: int = Field(default=60, ge=1, le=240)


def _tenant(message: Any) -> Tenant:
    return Tenant(
        tenant_id=message.tenant_id,
        name=message.name,
        status=message.status,
        created_at=time_of(message, "created_at"),
    )


def _grant(message: Any) -> BreakGlassGrant:
    return BreakGlassGrant(
        grant_id=message.grant_id,
        tenant_id=message.tenant_id,
        reason=message.reason,
        started_at=time_of(message, "started_at"),
        expires_at=time_of(message, "expires_at"),
        ended_at=time_of(message, "ended_at"),
    )


def add_platform_routes(router: APIRouter, caller: Any, call: Call) -> None:
    """Add the routes to the versioned router."""
    Who = Annotated[Caller, Depends(caller)]  # noqa: N806  # A type, named as one.

    @router.get("/platform/tenants", response_model=TenantPage, summary="Every tenant")
    async def list_tenants(
        who: Who,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(max_length=256)] = "",
    ) -> TenantPage:
        request = tenant_pb2.ListTenantsRequest(page_size=page_size, page_token=page_token)
        answer = await call("tenant", "ListTenants", request, who)
        return TenantPage(
            tenants=[_tenant(item) for item in answer.tenants],
            next_page_token=answer.next_page_token,
        )

    @router.post(
        "/platform/tenants",
        response_model=CreatedTenant,
        status_code=201,
        summary="Create a tenant, and its Keycloak organization",
    )
    async def create_tenant(who: Who, body: NewTenant, key: Key) -> CreatedTenant:
        request = tenant_pb2.CreateTenantRequest(
            target_tenant_id=body.tenant_id, name=body.name, idempotency_key=key
        )
        answer = await call("tenant", "CreateTenant", request, who, retry=False)
        return CreatedTenant(tenant=_tenant(answer.tenant), created=answer.created)

    @router.get("/platform/tenants/{tenant_id}", response_model=Tenant, summary="One tenant")
    async def get_tenant(who: Who, tenant_id: TenantId) -> Tenant:
        request = tenant_pb2.GetTenantRequest(target_tenant_id=tenant_id)
        answer = await call("tenant", "GetTenant", request, who)
        return _tenant(answer.tenant)

    @router.post(
        "/platform/tenants/{tenant_id}/suspend",
        response_model=Tenant,
        summary="Suspend a tenant, so its users can no longer sign in to it",
    )
    async def suspend_tenant(who: Who, tenant_id: TenantId, body: Suspension, key: Key) -> Tenant:
        request = tenant_pb2.SuspendTenantRequest(
            target_tenant_id=tenant_id, reason=body.reason, idempotency_key=key
        )
        answer = await call("tenant", "SuspendTenant", request, who, retry=False)
        return _tenant(answer.tenant)

    @router.post(
        "/platform/tenants/{tenant_id}/reactivate",
        response_model=Tenant,
        summary="Reactivate a suspended tenant",
    )
    async def reactivate_tenant(who: Who, tenant_id: TenantId, key: Key) -> Tenant:
        request = tenant_pb2.ReactivateTenantRequest(
            target_tenant_id=tenant_id, idempotency_key=key
        )
        answer = await call("tenant", "ReactivateTenant", request, who, retry=False)
        return _tenant(answer.tenant)

    @router.get(
        "/platform/tenants/{tenant_id}/branding",
        response_model=TenantBranding,
        summary="A tenant's branding and custom domain, to view",
    )
    async def tenant_branding(who: Who, tenant_id: TenantId) -> TenantBranding:
        request = tenant_pb2.GetBrandingRequest(target_tenant_id=tenant_id)
        answer = await call("tenant", "GetBranding", request, who)
        branding = answer.branding
        domain = (
            DomainStatus(domain=answer.custom_domain.domain, status=answer.custom_domain.status)
            if answer.HasField("custom_domain")
            else None
        )
        return TenantBranding(
            tenant_id=branding.tenant_id,
            display_name=branding.display_name,
            primary_color=branding.primary_color,
            accent_color=branding.accent_color,
            login_title=branding.login_title,
            login_message=branding.login_message,
            time_zone=branding.time_zone,
            locale=branding.locale,
            has_logo=branding.has_logo,
            updated_at=time_of(branding, "updated_at"),
            custom_domain=domain,
        )

    @router.get(
        "/platform/break-glass",
        response_model=BreakGlassGrants,
        summary="Your own active break-glass grants, soonest to expire first",
    )
    async def my_break_glass(who: Who) -> BreakGlassGrants:
        request = authorization_pb2.ListMyBreakGlassGrantsRequest()
        answer = await call("authorization", "ListMyBreakGlassGrants", request, who)
        return BreakGlassGrants(grants=[_grant(item) for item in answer.grants])

    @router.post(
        "/platform/break-glass",
        response_model=BreakGlassGrant,
        status_code=201,
        summary="Start reasoned, time-limited, read-only access to one tenant's data",
    )
    async def start_break_glass(who: Who, body: NewBreakGlass, key: Key) -> BreakGlassGrant:
        request = authorization_pb2.StartBreakGlassRequest(
            target_tenant_id=body.tenant_id,
            reason=body.reason,
            duration_minutes=body.duration_minutes,
            idempotency_key=key,
        )
        answer = await call("authorization", "StartBreakGlass", request, who, retry=False)
        return _grant(answer.grant)

    @router.post(
        "/platform/break-glass/{grant_id}/end",
        response_model=BreakGlassGrant,
        summary="End a break-glass grant early",
    )
    async def end_break_glass(
        who: Who, grant_id: Annotated[str, Path(pattern=ID_PATTERN)], key: Key
    ) -> BreakGlassGrant:
        request = authorization_pb2.EndBreakGlassRequest(grant_id=grant_id, idempotency_key=key)
        answer = await call("authorization", "EndBreakGlass", request, who, retry=False)
        return _grant(answer.grant)
