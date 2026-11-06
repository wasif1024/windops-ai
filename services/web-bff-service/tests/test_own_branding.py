"""A tenant's own branding routes, through real HTTP requests with a fake Tenant Service."""

from typing import Any

import grpc
from test_admin import KEY, FakeService
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.tenant.v1 import tenant_pb2
from windops_auth import STEP_UP_MESSAGE
from windops_web_bff.api import PREFIX
from windops_web_bff.branding import LOGO_MAXIMUM_BYTES

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BRANDING = tenant_pb2.GetBrandingResponse(
    branding=tenant_pb2.Branding(tenant_id="north-sea", display_name="North Sea", has_logo=True),
    custom_domain=tenant_pb2.CustomDomain(
        domain="ops.north-sea.example", status="pending", verification_token="proof-123"
    ),
)
CHANGE = {
    "display_name": "North Sea Wind",
    "primary_color": "#0b5394",
    "accent_color": "#38761d",
    "login_title": "Sign in",
    "login_message": "Authorized staff only",
    "time_zone": "Europe/Oslo",
    "locale": "en-GB",
}


def tenant_service(**answers: Any) -> FakeService:
    return FakeService({"GetBranding": BRANDING, **answers})


def test_a_tenant_administrator_sees_the_domain_proof_they_must_publish() -> None:
    tenant = tenant_service()

    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/admin/branding", headers=auth(token()))
        assert response.json()["custom_domain"]["verification_token"] == "proof-123"
        # Their own tenant, from their verified identity: no tenant is named.
        assert tenant.calls[0].request.target_tenant_id == ""

    with_bff(scenario, services={"tenant": tenant})


def test_branding_is_replaced_in_full_and_the_new_branding_returned() -> None:
    tenant = tenant_service(UpdateBranding=tenant_pb2.UpdateBrandingResponse())

    async def scenario(world: World) -> None:
        response = await world.client.put(
            f"{PREFIX}/admin/branding", json=CHANGE, headers={**auth(token()), **KEY}
        )
        assert response.status_code == 200
        update = tenant.calls[0]
        assert (update.method, update.retry, update.request.time_zone) == (
            "UpdateBranding",
            False,
            "Europe/Oslo",
        )
        assert tenant.calls[1].method == "GetBranding"

    with_bff(scenario, services={"tenant": tenant})


def test_a_logo_is_sent_as_its_bytes() -> None:
    tenant = tenant_service(SetLogo=tenant_pb2.SetLogoResponse())

    async def scenario(world: World) -> None:
        response = await world.client.put(
            f"{PREFIX}/admin/branding/logo",
            content=PNG,
            headers={**auth(token()), **KEY, "Content-Type": "image/png"},
        )
        assert response.status_code == 200
        logo = tenant.calls[0]
        assert (logo.request.image, logo.request.content_type) == (PNG, "image/png")

    with_bff(scenario, services={"tenant": tenant})


def test_a_logo_of_another_type_or_too_big_never_reaches_the_service() -> None:
    tenant = tenant_service()

    async def scenario(world: World) -> None:
        headers = {**auth(token()), **KEY}
        svg = await world.client.put(
            f"{PREFIX}/admin/branding/logo",
            content=b"<svg onload=alert(1)>",
            headers={**headers, "Content-Type": "image/svg+xml"},
        )
        assert (svg.status_code, svg.json()["code"]) == (415, "unsupported_media_type")
        big = await world.client.put(
            f"{PREFIX}/admin/branding/logo",
            content=PNG + b"\x00" * LOGO_MAXIMUM_BYTES,
            headers={**headers, "Content-Type": "image/png"},
        )
        assert (big.status_code, big.json()["code"]) == (413, "too_large")
        assert tenant.calls == []

    with_bff(scenario, services={"tenant": tenant})


def test_templates_and_the_domain_are_changed_with_their_keys() -> None:
    template = tenant_pb2.EmailTemplate(
        template="invitation", subject="Welcome", body="Hi {{user_name}}", customized=True
    )
    tenant = FakeService(
        {
            "SetEmailTemplate": tenant_pb2.SetEmailTemplateResponse(template=template),
            "SetCustomDomain": tenant_pb2.SetCustomDomainResponse(
                custom_domain=BRANDING.custom_domain
            ),
            "RemoveCustomDomain": tenant_pb2.RemoveCustomDomainResponse(),
        }
    )

    async def scenario(world: World) -> None:
        headers = {**auth(token()), **KEY}
        changed = await world.client.put(
            f"{PREFIX}/admin/email-templates/invitation",
            json={"subject": "Welcome", "body": "Hi {{user_name}}"},
            headers=headers,
        )
        assert changed.json()["customized"] is True
        domain = await world.client.put(
            f"{PREFIX}/admin/custom-domain",
            json={"domain": "ops.north-sea.example"},
            headers=headers,
        )
        assert domain.json()["status"] == "pending"
        removed = await world.client.delete(f"{PREFIX}/admin/custom-domain", headers=headers)
        assert removed.status_code == 204
        assert [call.method for call in tenant.calls] == [
            "SetEmailTemplate",
            "SetCustomDomain",
            "RemoveCustomDomain",
        ]
        assert all(call.retry is False for call in tenant.calls)

    with_bff(scenario, services={"tenant": tenant})


def test_a_change_without_a_recent_second_factor_asks_for_one() -> None:
    tenant = FakeService(error=rpc_error(grpc.StatusCode.PERMISSION_DENIED, STEP_UP_MESSAGE))

    async def scenario(world: World) -> None:
        response = await world.client.put(
            f"{PREFIX}/admin/branding", json=CHANGE, headers={**auth(token()), **KEY}
        )
        assert response.json()["code"] == "step_up_required"

    with_bff(scenario, services={"tenant": tenant})
