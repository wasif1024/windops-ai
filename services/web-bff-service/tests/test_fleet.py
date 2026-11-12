"""The fleet screens' API, through real HTTP requests with fake services behind it (M4.8)."""

from typing import Any

import grpc
from test_admin import FakeService, stamp
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.asset.v1 import asset_pb2
from windops.telemetry.v1 import telemetry_pb2 as tpb
from windops_web_bff.api import PREFIX

FARM = asset_pb2.WindFarm(
    wind_farm_id="dogger-east", name="Dogger East", country="GB", offshore=True,
    time_zone="Europe/London", location=asset_pb2.Location(latitude=54.8, longitude=1.9),
)  # fmt: skip
TURBINE = asset_pb2.Turbine(
    turbine_id="dogger-east-t01", wind_farm_id="dogger-east", name="T01", status="operating",
    model=asset_pb2.TurbineModel(manufacturer="Vestas", model="V164", rated_power_kw=9500),
    serial_number="SN-1", commissioned_on="2024-05-17", status_changed_at=stamp(1_767_225_600),
)  # fmt: skip


def point(
    metric: int, value: float, quality: int = tpb.QUALITY_GOOD, at: int = 1_767_225_600,
    label: str = "ok",
) -> Any:  # fmt: skip
    return tpb.TelemetryPoint(
        event_time=stamp(at), metric=metric, value=value, unit="kW", quality=quality,  # type: ignore[arg-type]
        sequence=at, quality_label=label,
    )  # fmt: skip


LATEST = tpb.LatestTelemetry(
    turbine_id="dogger-east-t01", event_time=stamp(1_767_225_600),
    ingested_at=stamp(1_767_225_602), operating_state="producing", available=True,
    points=[
        point(tpb.METRIC_POWER, 7200.0), point(tpb.METRIC_WIND_SPEED, 0.0, tpb.QUALITY_MISSING),
    ],
)  # fmt: skip


def assets() -> FakeService:
    return FakeService(
        {
            "ListWindFarms": asset_pb2.ListWindFarmsResponse(wind_farms=[FARM]),
            "ListTurbines": asset_pb2.ListTurbinesResponse(turbines=[TURBINE]),
            "GetTurbine": asset_pb2.GetTurbineResponse(
                turbine=TURBINE,
                components=[
                    asset_pb2.Component(
                        component_id="c-1",
                        turbine_id="dogger-east-t01",
                        kind="gearbox",
                        name="Gearbox",
                        status="in_service",
                    )
                ],
            ),
        }
    )


def test_the_fleet_shows_each_farm_and_turbine_with_its_latest_readings() -> None:
    telemetry = FakeService(
        {"GetLatestTelemetry": tpb.GetLatestTelemetryResponse(turbines=[LATEST])}
    )
    asset = assets()

    async def scenario(world: World) -> None:
        answer = await world.client.get(f"{PREFIX}/fleet", headers=auth(token()))
        assert answer.status_code == 200
        body = answer.json()
        assert body["telemetry_available"] is True
        (farm,) = body["wind_farms"]
        assert (farm["name"], farm["offshore"], farm["location"]["latitude"]) == (
            "Dogger East",
            True,
            54.8,
        )
        (turbine,) = farm["turbines"]
        assert (turbine["status"], turbine["rated_power_kw"]) == ("operating", 9500)
        latest = turbine["latest"]
        assert latest["delay_seconds"] == 2 and latest["operating_state"] == "producing"
        assert latest["readings"] == [
            {"metric": "power", "value": 7200.0, "unit": "kW", "quality": "good"},
            {"metric": "wind_speed", "value": None, "unit": "kW", "quality": "missing"},
        ]
        # Each service is asked as the signed-in user, so it decides what they may see.
        assert {call.user_id for call in [*asset.calls, *telemetry.calls]} == {"u-1"}
        assert list(telemetry.calls[0].request.turbine_ids) == ["dogger-east-t01"]

    with_bff(scenario, services={"asset": asset, "telemetry": telemetry})


def test_the_fleet_is_still_shown_when_telemetry_cannot_be_reached() -> None:
    telemetry = FakeService(error=rpc_error(grpc.StatusCode.UNAVAILABLE, "down"))

    async def scenario(world: World) -> None:
        body = (await world.client.get(f"{PREFIX}/fleet", headers=auth(token()))).json()
        assert body["telemetry_available"] is False
        assert body["wind_farms"][0]["turbines"][0]["latest"] is None

    with_bff(scenario, services={"asset": assets(), "telemetry": telemetry})


def test_a_turbine_shows_its_model_components_and_latest_readings() -> None:
    telemetry = FakeService(
        {"GetLatestTelemetry": tpb.GetLatestTelemetryResponse(turbines=[LATEST])}
    )

    async def scenario(world: World) -> None:
        answer = await world.client.get(
            f"{PREFIX}/fleet/turbines/dogger-east-t01", headers=auth(token())
        )
        body = answer.json()
        assert body["model"]["model"] == "V164" and body["commissioned_on"] == "2024-05-17"
        assert body["status_changed_at"] == "2026-01-01T00:00:00Z"
        assert [c["kind"] for c in body["components"]] == ["gearbox"]
        assert body["latest"]["readings"][0]["value"] == 7200.0

    with_bff(scenario, services={"asset": assets(), "telemetry": telemetry})


def test_a_turbine_the_user_may_not_read_is_not_found() -> None:
    asset = FakeService(error=rpc_error(grpc.StatusCode.NOT_FOUND, "No such turbine"))

    async def scenario(world: World) -> None:
        answer = await world.client.get(
            f"{PREFIX}/fleet/turbines/dogger-east-t01", headers=auth(token())
        )
        assert answer.status_code == 404

    with_bff(scenario, services={"asset": asset, "telemetry": FakeService()})


def test_a_chart_reads_the_last_minutes_of_the_metrics_asked_for() -> None:
    points = [
        point(tpb.METRIC_POWER, 7100.0, at=1_767_225_600),
        point(tpb.METRIC_POWER, 7200.0, tpb.QUALITY_SUSPECT, at=1_767_225_601, label="delayed"),
        point(tpb.METRIC_POWER, 7300.0, tpb.QUALITY_SUSPECT, at=1_767_225_602, label="received"),
    ]
    telemetry = FakeService({"StreamTurbineTelemetry": points})

    async def scenario(world: World) -> None:
        answer = await world.client.get(
            f"{PREFIX}/fleet/turbines/dogger-east-t01/telemetry?minutes=5&metric=power&metric=gearbox_temperature",
            headers=auth(token()),
        )
        body = answer.json()
        power, gearbox = body["series"]
        assert power["metric"] == "power" and power["unit"] == "kW"
        assert [(p["value"], p["quality"]) for p in power["points"]] == [
            (7100.0, "good"),
            (7200.0, "suspect"),
            (7300.0, "unchecked"),  # not yet checked, so not marked as suspect
        ]
        assert gearbox == {"metric": "gearbox_temperature", "unit": "", "points": []}
        request = telemetry.calls[0].request
        assert list(request.metrics) == [tpb.METRIC_POWER, tpb.METRIC_GEARBOX_TEMPERATURE]
        window = request.to_time.ToDatetime() - request.from_time.ToDatetime()
        assert window.total_seconds() == 300

    with_bff(scenario, services={"asset": assets(), "telemetry": telemetry})


def test_a_chart_refuses_an_unknown_metric_or_a_window_over_an_hour() -> None:
    async def scenario(world: World) -> None:
        base = f"{PREFIX}/fleet/turbines/dogger-east-t01/telemetry"
        for query in ("?metric=nonsense", "?minutes=61", "?minutes=0"):
            answer = await world.client.get(base + query, headers=auth(token()))
            assert answer.status_code == 422, query

    with_bff(scenario, services={"asset": assets(), "telemetry": FakeService()})
