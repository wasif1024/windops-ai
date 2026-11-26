"""Production impact and the weather through real HTTP requests, with fake Production Impact and
Weather services (M11.3)."""

from test_admin import FakeService, stamp
from test_web_bff import World, auth, token, with_bff
from windops.production_impact.v1 import production_impact_pb2 as imp
from windops.weather.v1 import weather_pb2 as wth
from windops_web_bff.api import PREFIX

RESULT = imp.ImpactResult(
    result_id="imp_1", case_id="case_1", turbine_id="dogger-east-t01", action="repair",
    downtime_hours=6, formula_version="impact-formulas-1", verified=True,
    inputs=[imp.ImpactInput(name="observed.mean_wind_mps", value_json="8.2", source="telemetry",
                            source_ref="tsnap_1", observed_at=stamp(1_767_225_600))],
    figures=[imp.Figure(name="projected_lost_energy_mwh", value=9.1, low=7.3, high=10.9,
                        unit="MWh")],
    assumptions=["The observed wind holds for the downtime"],
)  # fmt: skip


def test_a_case_estimates_are_listed_read_and_made_as_the_person() -> None:
    impact = FakeService({
        "ListImpactResults": imp.ListImpactResultsResponse(results=[RESULT]),
        "GetImpactResult": imp.GetImpactResultResponse(result=RESULT),
        "EstimateImpact": imp.EstimateImpactResponse(result=RESULT),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        headers = auth(token())
        listed = (await world.client.get(f"{PREFIX}/impact?case_id=case_1", headers=headers)).json()
        [figure] = listed[0]["figures"]
        assert (figure["value"], figure["low"], figure["high"]) == (9.1, 7.3, 10.9)
        assert listed[0]["inputs"][0]["value"] == 8.2, "the input's value as its JSON"
        one = (await world.client.get(f"{PREFIX}/impact/imp_1", headers=headers)).json()
        assert one["verified"] and one["assumptions"]
        made = await world.client.post(
            f"{PREFIX}/impact", headers={**headers, "Idempotency-Key": "e-1"},
            json={"case_id": "case_1", "action": "repair", "downtime_hours": 6,
                  "planned_start": "2026-12-01T08:00:00Z"},
        )  # fmt: skip
        assert made.status_code == 201
        sent = next(c for c in impact.calls if c.method == "EstimateImpact")
        assert (sent.request.downtime_hours, sent.request.idempotency_key, sent.retry) == (
            6, "e-1", False)  # fmt: skip
        assert sent.request.planned_start.ToSeconds() == 1_796_112_000
        refused = await world.client.post(
            f"{PREFIX}/impact", headers={**headers, "Idempotency-Key": "e-2"},
            json={"case_id": "case_1", "action": "rebuild", "downtime_hours": 0},
        )  # fmt: skip
        assert refused.status_code == 422

    with_bff(scenario, services={"production_impact": impact})


def test_a_forecast_says_how_fresh_it_is() -> None:
    weather = FakeService({"GetForecast": wth.GetForecastResponse(
        turbine_id="dogger-east-t01", state="stale", note="The provider cannot be reached",
        age_seconds=14_400,
        forecast=wth.Forecast(forecast_id="fct_1", provider="open-meteo", model="best_match",
                              licence="CC BY 4.0", retrieved_at=stamp(1_767_225_600)),
        summary=wth.ForecastSummary(max_wind_speed_hub_mps=11.5, hours_covered=24,
                                    hours_wanted=24),
        points=[wth.ForecastPoint(valid_at=stamp(1_767_229_200), wind_speed_hub_mps=9.4)],
    )})  # fmt: skip

    async def scenario(world: World) -> None:
        got = (await world.client.get(f"{PREFIX}/weather/dogger-east-t01?hours=48",
                                      headers=auth(token()))).json()  # fmt: skip
        assert (got["state"], got["age_seconds"], got["licence"]) == ("stale", 14_400, "CC BY 4.0")
        assert got["summary"]["max_wind_speed_hub_mps"] == 11.5
        assert got["summary"]["max_cape_j_per_kg"] is None, "only what the provider supplied"
        assert got["points"][0]["wind_speed_hub_mps"] == 9.4
        [call] = weather.calls
        span = call.request.to_time.ToSeconds() - call.request.from_time.ToSeconds()
        assert span == 48 * 3600
        too_far = await world.client.get(f"{PREFIX}/weather/dogger-east-t01?hours=500",
                                         headers=auth(token()))  # fmt: skip
        assert too_far.status_code == 422

    with_bff(scenario, services={"weather": weather})
