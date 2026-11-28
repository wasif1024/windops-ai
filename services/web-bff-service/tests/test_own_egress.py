"""A tenant's egress switches, through real HTTP requests with a fake Tenant Service (EGR-002)."""

import grpc
from test_admin import KEY, FakeService
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.tenant.v1 import tenant_pb2
from windops_web_bff.api import PREFIX

FORECASTS = tenant_pb2.EgressPath(
    path="forecasts", enabled=True,
    destination="Forecast provider: a site's coordinates and a time window",
    when_off="No new forecasts.",
)  # fmt: skip
OFF = tenant_pb2.EgressPath(
    path="forecasts", enabled=False, destination=FORECASTS.destination,
    when_off=FORECASTS.when_off, reason="Coordinates not yet approved", updated_by="u-admin",
)  # fmt: skip
TURN_OFF = {"enabled": False, "reason": "Coordinates not yet approved"}


def test_an_administrator_sees_each_path_and_what_stops_while_it_is_off() -> None:
    tenant = FakeService({"GetEgress": tenant_pb2.GetEgressResponse(paths=[FORECASTS])})

    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/admin/egress", headers=auth(token()))
        assert response.status_code == 200
        (path,) = response.json()["paths"]
        assert (path["path"], path["enabled"], path["when_off"], path["updated_at"]) == (
            "forecasts", True, "No new forecasts.", None,
        )  # fmt: skip
        # Their own tenant, from their verified identity: no tenant is named.
        assert tenant.calls[0].request.target_tenant_id == ""

    with_bff(scenario, services={"tenant": tenant})


def test_a_path_is_turned_off_with_its_reason_and_never_retried() -> None:
    tenant = FakeService({"SetEgress": tenant_pb2.SetEgressResponse(path=OFF)})

    async def scenario(world: World) -> None:
        response = await world.client.put(
            f"{PREFIX}/admin/egress/forecasts", json=TURN_OFF, headers={**auth(token()), **KEY}
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is False
        change = tenant.calls[0]
        assert (change.method, change.retry, change.request.path, change.request.reason) == (
            "SetEgress", False, "forecasts", "Coordinates not yet approved",
        )  # fmt: skip

    with_bff(scenario, services={"tenant": tenant})


def test_a_short_reason_is_refused_before_the_tenant_service() -> None:
    tenant = FakeService({})

    async def scenario(world: World) -> None:
        response = await world.client.put(
            f"{PREFIX}/admin/egress/forecasts",
            json={"enabled": False, "reason": "no"},
            headers={**auth(token()), **KEY},
        )
        assert response.status_code == 422
        assert tenant.calls == []

    with_bff(scenario, services={"tenant": tenant})


def test_without_permission_the_tenant_services_refusal_is_passed_on() -> None:
    refused = rpc_error(grpc.StatusCode.PERMISSION_DENIED, "You do not have permission to do this")
    tenant = FakeService(error=refused)

    async def scenario(world: World) -> None:
        response = await world.client.put(
            f"{PREFIX}/admin/egress/claude", json=TURN_OFF, headers={**auth(token()), **KEY}
        )
        assert response.status_code == 403

    with_bff(scenario, services={"tenant": tenant})
