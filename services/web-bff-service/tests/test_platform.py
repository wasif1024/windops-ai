"""The Platform console's API, through real HTTP requests with fake services behind it."""

from typing import Any

import grpc
import pytest
from test_admin import KEY, FakeService, stamp
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.authorization.v1 import authorization_pb2 as authz
from windops.tenant.v1 import tenant_pb2
from windops_web_bff.api import PREFIX

ATLAS = tenant_pb2.Tenant(
    tenant_id="atlas-wind", name="Atlas Wind", status="active", created_at=stamp(1_767_225_600)
)
REASON = "Investigating incident INC-4411 for the tenant"


def platform_token() -> str:
    return token(tenant_id="windops-platform", sub="pa-1")


def test_tenants_are_listed_and_one_is_read_by_name() -> None:
    tenant = FakeService(
        {
            "ListTenants": tenant_pb2.ListTenantsResponse(tenants=[ATLAS]),
            "GetTenant": tenant_pb2.GetTenantResponse(tenant=ATLAS),
        }
    )

    async def scenario(world: World) -> None:
        listed = await world.client.get(
            f"{PREFIX}/platform/tenants", headers=auth(platform_token())
        )
        assert listed.json() == {
            "tenants": [
                {
                    "tenant_id": "atlas-wind",
                    "name": "Atlas Wind",
                    "status": "active",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
            "next_page_token": "",
        }
        one = await world.client.get(
            f"{PREFIX}/platform/tenants/atlas-wind", headers=auth(platform_token())
        )
        assert one.json()["name"] == "Atlas Wind"
        # The named tenant goes as the target. The service checks the caller may reach it.
        assert tenant.calls[1].request.target_tenant_id == "atlas-wind"
        assert tenant.calls[1].user_id == "pa-1"

    with_bff(scenario, services={"tenant": tenant})


def test_a_tenant_is_created_and_suspended_with_its_key_and_never_retried_here() -> None:
    tenant = FakeService(
        {
            "CreateTenant": tenant_pb2.CreateTenantResponse(tenant=ATLAS, created=True),
            "SuspendTenant": tenant_pb2.SuspendTenantResponse(tenant=ATLAS),
        }
    )

    async def scenario(world: World) -> None:
        headers = {**auth(platform_token()), **KEY}
        created = await world.client.post(
            f"{PREFIX}/platform/tenants",
            json={"tenant_id": "atlas-wind", "name": "Atlas Wind"},
            headers=headers,
        )
        assert created.status_code == 201 and created.json()["created"] is True
        suspended = await world.client.post(
            f"{PREFIX}/platform/tenants/atlas-wind/suspend",
            json={"reason": "Contract ended in October"},
            headers=headers,
        )
        assert suspended.status_code == 200
        create, suspend = tenant.calls
        assert (create.request.target_tenant_id, create.retry) == ("atlas-wind", False)
        assert (suspend.request.reason, suspend.retry) == ("Contract ended in October", False)
        assert suspend.request.idempotency_key == KEY["Idempotency-Key"]

    with_bff(scenario, services={"tenant": tenant})


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/platform/tenants", {"tenant_id": "Not A Tenant", "name": "x"}),
        ("/platform/tenants/atlas-wind/suspend", {"reason": "too short"}),
        ("/platform/break-glass", {"tenant_id": "atlas-wind", "reason": "short"}),
        (
            "/platform/break-glass",
            {"tenant_id": "atlas-wind", "reason": REASON, "duration_minutes": 241},
        ),
    ],
)
def test_a_bad_request_never_reaches_a_service(path: str, body: dict[str, Any]) -> None:
    services = {"tenant": FakeService(), "authorization": FakeService()}

    async def scenario(world: World) -> None:
        response = await world.client.post(
            f"{PREFIX}{path}", json=body, headers={**auth(platform_token()), **KEY}
        )
        assert response.status_code == 422
        assert not services["tenant"].calls and not services["authorization"].calls

    with_bff(scenario, services=services)


def test_a_tenants_branding_is_shown_without_its_domain_proof() -> None:
    tenant = FakeService(
        {
            "GetBranding": tenant_pb2.GetBrandingResponse(
                branding=tenant_pb2.Branding(tenant_id="atlas-wind", display_name="Atlas"),
                custom_domain=tenant_pb2.CustomDomain(
                    domain="ops.atlas.example", status="pending", verification_token="proof-123"
                ),
            )
        }
    )

    async def scenario(world: World) -> None:
        response = await world.client.get(
            f"{PREFIX}/platform/tenants/atlas-wind/branding", headers=auth(platform_token())
        )
        assert response.json()["custom_domain"] == {
            "domain": "ops.atlas.example",
            "status": "pending",
        }
        assert "proof-123" not in response.text

    with_bff(scenario, services={"tenant": tenant})


def test_break_glass_is_started_listed_and_ended() -> None:
    grant = authz.BreakGlassGrant(
        grant_id="bg-1",
        tenant_id="atlas-wind",
        user_id="pa-1",
        reason=REASON,
        started_at=stamp(1_767_225_600),
        expires_at=stamp(1_767_229_200),
    )
    authorization = FakeService(
        {
            "StartBreakGlass": authz.StartBreakGlassResponse(grant=grant),
            "ListMyBreakGlassGrants": authz.ListMyBreakGlassGrantsResponse(grants=[grant]),
            "EndBreakGlass": authz.EndBreakGlassResponse(grant=grant),
        }
    )

    async def scenario(world: World) -> None:
        headers = {**auth(platform_token()), **KEY}
        started = await world.client.post(
            f"{PREFIX}/platform/break-glass",
            json={"tenant_id": "atlas-wind", "reason": REASON, "duration_minutes": 60},
            headers=headers,
        )
        assert started.status_code == 201
        assert started.json()["expires_at"] == "2026-01-01T01:00:00Z"
        listed = await world.client.get(
            f"{PREFIX}/platform/break-glass", headers=auth(platform_token())
        )
        assert [item["grant_id"] for item in listed.json()["grants"]] == ["bg-1"]
        ended = await world.client.post(f"{PREFIX}/platform/break-glass/bg-1/end", headers=headers)
        assert ended.status_code == 200
        start, _, end = authorization.calls
        assert (start.request.target_tenant_id, start.request.duration_minutes) == (
            "atlas-wind",
            60,
        )
        assert (start.retry, end.retry, end.request.grant_id) == (False, False, "bg-1")

    with_bff(scenario, services={"authorization": authorization})


def test_a_tenant_administrator_is_refused_by_the_service_in_our_own_words() -> None:
    tenant = FakeService(error=rpc_error(grpc.StatusCode.PERMISSION_DENIED, "needs tenant.manage"))

    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/platform/tenants", headers=auth(token()))
        assert response.status_code == 403
        assert "tenant.manage" not in response.text

    with_bff(scenario, services={"tenant": tenant})
