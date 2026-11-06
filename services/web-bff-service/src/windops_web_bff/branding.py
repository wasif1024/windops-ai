"""A tenant's own branding, for its administrators' console (SRS 9.4, FR-TEN-002).

It decides nothing: the Tenant Service needs branding.manage, which SRS 3.2 gives to Tenant
Administrators, and a recent second factor for every change. It checks every value too.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field
from windops.tenant.v1 import tenant_pb2

from windops_web_bff.common import Call, Key, time_of
from windops_web_bff.errors import problem
from windops_web_bff.tokens import Caller

# The Tenant Service's own limit, so a bigger upload is refused before it is read in full.
LOGO_MAXIMUM_BYTES = 256 * 1024
LOGO_TYPES = frozenset({"image/png", "image/jpeg"})


class CustomDomain(BaseModel):
    domain: str
    # "pending" until it is proven, which a later module adds.
    status: str
    # To publish in a DNS TXT record, as proof of control.
    verification_token: str
    created_at: str | None


class OwnBranding(BaseModel):
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
    custom_domain: CustomDomain | None


class BrandingChange(BaseModel):
    """Every field, as the Tenant Service replaces them all. It checks each one."""

    display_name: str = Field(max_length=200)
    primary_color: str = Field(max_length=7)
    accent_color: str = Field(max_length=7)
    login_title: str = Field(max_length=200)
    login_message: str = Field(max_length=1000)
    time_zone: str = Field(max_length=64)
    locale: str = Field(max_length=35)


class EmailTemplate(BaseModel):
    template: str
    subject: str
    body: str
    # False while it is the platform's default text.
    customized: bool
    placeholders: list[str]


class EmailTemplates(BaseModel):
    templates: list[EmailTemplate]


class TemplateChange(BaseModel):
    subject: str = Field(max_length=200)
    body: str = Field(max_length=10_000)


class DomainChange(BaseModel):
    domain: str = Field(min_length=3, max_length=253)


def _domain(message: Any) -> CustomDomain:
    return CustomDomain(
        domain=message.domain,
        status=message.status,
        verification_token=message.verification_token,
        created_at=time_of(message, "created_at"),
    )


def _branding(message: Any, domain: Any | None) -> OwnBranding:
    return OwnBranding(
        tenant_id=message.tenant_id,
        display_name=message.display_name,
        primary_color=message.primary_color,
        accent_color=message.accent_color,
        login_title=message.login_title,
        login_message=message.login_message,
        time_zone=message.time_zone,
        locale=message.locale,
        has_logo=message.has_logo,
        updated_at=time_of(message, "updated_at"),
        custom_domain=_domain(domain) if domain is not None else None,
    )


def _template(message: Any) -> EmailTemplate:
    return EmailTemplate(
        template=message.template,
        subject=message.subject,
        body=message.body,
        customized=message.customized,
        placeholders=list(message.placeholders),
    )


async def _bounded_body(request: Request) -> bytes | None:
    """The upload, or None once it passes the limit, without reading the rest into memory."""
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > LOGO_MAXIMUM_BYTES:
            return None
    return bytes(received)


def add_branding_routes(router: APIRouter, caller: Any, call: Call, fail: Any) -> None:
    """Add the routes to the versioned router. `fail` turns a problem into a raisable error."""
    Who = Annotated[Caller, Depends(caller)]  # noqa: N806  # A type, named as one.

    async def current(who: Caller) -> OwnBranding:
        # The proof token too, which the Tenant Service gives only to those with branding.manage.
        request = tenant_pb2.GetBrandingRequest(include_verification_token=True)
        answer = await call("tenant", "GetBranding", request, who)
        domain = answer.custom_domain if answer.HasField("custom_domain") else None
        return _branding(answer.branding, domain)

    @router.get(
        "/admin/branding",
        response_model=OwnBranding,
        summary="Your tenant's branding, with its custom domain's proof token",
    )
    async def get_branding(who: Who) -> OwnBranding:
        return await current(who)

    @router.put("/admin/branding", response_model=OwnBranding, summary="Change the branding")
    async def update_branding(who: Who, body: BrandingChange, key: Key) -> OwnBranding:
        request = tenant_pb2.UpdateBrandingRequest(**body.model_dump(), idempotency_key=key)
        await call("tenant", "UpdateBranding", request, who, retry=False)
        return await current(who)

    @router.put(
        "/admin/branding/logo",
        response_model=OwnBranding,
        summary="Replace the logo: a PNG or JPEG of at most 256 KiB, sent as the body",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    kind: {"schema": {"type": "string", "format": "binary"}} for kind in LOGO_TYPES
                },
            }
        },
    )
    async def set_logo(who: Who, request: Request, key: Key) -> OwnBranding:
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind not in LOGO_TYPES:
            raise fail(problem(415, "A logo is PNG or JPEG", "unsupported_media_type"))
        image = await _bounded_body(request)
        if image is None:
            raise fail(problem(413, "A logo is at most 256 KiB", "too_large"))
        change = tenant_pb2.SetLogoRequest(image=image, content_type=kind, idempotency_key=key)
        await call("tenant", "SetLogo", change, who, retry=False)
        return await current(who)

    @router.get(
        "/admin/email-templates",
        response_model=EmailTemplates,
        summary="The emails your tenant's users get, and the placeholders each may use",
    )
    async def list_templates(who: Who) -> EmailTemplates:
        answer = await call(
            "tenant", "ListEmailTemplates", tenant_pb2.ListEmailTemplatesRequest(), who
        )
        return EmailTemplates(templates=[_template(item) for item in answer.templates])

    @router.put(
        "/admin/email-templates/{template}",
        response_model=EmailTemplate,
        summary="Change one email's subject and text",
    )
    async def set_template(
        who: Who,
        template: Annotated[str, Path(pattern=r"^[a-z_]{1,40}$")],
        body: TemplateChange,
        key: Key,
    ) -> EmailTemplate:
        request = tenant_pb2.SetEmailTemplateRequest(
            template=template, subject=body.subject, body=body.body, idempotency_key=key
        )
        answer = await call("tenant", "SetEmailTemplate", request, who, retry=False)
        return _template(answer.template)

    @router.put(
        "/admin/custom-domain",
        response_model=CustomDomain,
        summary="Set your tenant's one custom domain, with a token to prove it",
    )
    async def set_domain(who: Who, body: DomainChange, key: Key) -> CustomDomain:
        request = tenant_pb2.SetCustomDomainRequest(domain=body.domain, idempotency_key=key)
        answer = await call("tenant", "SetCustomDomain", request, who, retry=False)
        return _domain(answer.custom_domain)

    @router.delete(
        "/admin/custom-domain", status_code=204, summary="Remove your tenant's custom domain"
    )
    async def remove_domain(who: Who, key: Key) -> None:
        request = tenant_pb2.RemoveCustomDomainRequest(idempotency_key=key)
        await call("tenant", "RemoveCustomDomain", request, who, retry=False)
