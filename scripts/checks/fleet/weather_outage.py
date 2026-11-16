#!/usr/bin/env python3
"""Prove a missing weather input fails closed, on the cluster (M6.5, FR-POL-005, SRS 7.7).

The Phase 6 gate's second half. Run the platform first, as `make dev` does. A check-only turbine
at a site of its own, in the tenant `telemetry-check`, has a case, and a proposal to climb it is
judged as the weather goes missing in each way it can:

1. the provider cannot be reached, and nothing is kept for the site: the forecast is
   unavailable, and the climb blocks;
2. the provider is still down, and the only forecast kept is 4 hours old: it is stale, never
   presented as current, and the climb blocks, though that forecast's wind is calm;
3. the provider is back: the forecast is fresh, and the climb is judged on it;
4. the Weather Service itself is down: the climb blocks, on this turbine and on a demo one, and
   the Production Impact Service holds the observed wind instead of guessing a forecast;
5. the Weather Service is back, and so is the judgement.

The provider is made unreachable by pointing the Weather Service at a closed address, and the
service is taken down by scaling it to zero. Both are put back however the check ends. Its
turbine, case, forecasts, results and estimates are removed at the end.
"""

import contextlib
import json
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.investigation.v1 import investigation_pb2, investigation_pb2_grpc
from windops.policy.v1 import policy_pb2, policy_pb2_grpc
from windops.production_impact.v1 import production_impact_pb2, production_impact_pb2_grpc
from windops.weather.v1 import weather_pb2, weather_pb2_grpc

from scripts.checks.fleet import asset, cases
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
HOSTS = {
    name: f"windops-{name}.windops-application.svc.cluster.local"
    for name in ("asset", "investigation", "policy", "production-impact", "weather")
}
TENANT, DEMO_TENANT, DEMO_TURBINE = "telemetry-check", "north-sea", "dogger-east-t01"
RUN = secrets.token_hex(3)
TURBINE, FARM = f"weather-gate-{RUN}", "weather-gate-farm"
ADMIN, ENGINEER, OPERATOR = "weather-gate-admin", "weather-gate-engineer", "weather-gate-operator"
# A site of its own each run, at sea, so nothing is kept for it yet.
LATITUDE = round(56.0 + secrets.randbelow(90) / 100, 2)
LONGITUDE = round(4.0 + secrets.randbelow(90) / 100, 2)
TITLE = f"Case check weather {RUN}"
# Nothing listens here: the provider cannot be reached.
CLOSED = "https://127.0.0.1:9/v1/forecast"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def stamp(at: datetime) -> Timestamp:
    value = Timestamp()
    value.FromDatetime(at)
    return value


def sql(database: str, statement: str, tenant: str = TENANT) -> str:
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={database}", f"--dbname={database}", "--set=ON_ERROR_STOP=1",
        f"--command=SELECT set_config('windops.tenant_id', '{tenant}', false); {statement}",
        namespace=DATA,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def weather_deployment(*arguments: str) -> None:
    for command in (arguments, ("rollout", "status", "deployment/windops-weather",
                                "--timeout=180s")):  # fmt: skip
        done = kubectl(*command, namespace=APPLICATION)
        if done.returncode != 0:
            raise RuntimeError(f"kubectl failed: {done.stderr.strip()}")


def provider_closed(closed: bool) -> None:
    name = "WINDOPS_WEATHER_PROVIDER_URL"
    setting = f"{name}={CLOSED}" if closed else f"{name}-"
    weather_deployment("set", "env", "deployment/windops-weather", setting)


def service_down(down: bool) -> None:
    done = kubectl("scale", "deployment/windops-weather", f"--replicas={0 if down else 1}",
                   namespace=APPLICATION)  # fmt: skip
    if done.returncode != 0:
        raise RuntimeError(f"kubectl failed: {done.stderr.strip()}")
    if down:
        kubectl("wait", "--for=delete", "pod", "-l", "app.kubernetes.io/name=windops-weather",
                "--timeout=120s", namespace=APPLICATION)  # fmt: skip
    else:
        weather_deployment("get", "deployment/windops-weather")


def restore() -> None:
    """However the check ends, the provider and the service are as they were."""
    kubectl("set", "env", "deployment/windops-weather", "WINDOPS_WEATHER_PROVIDER_URL-",
            namespace=APPLICATION)  # fmt: skip
    kubectl("scale", "deployment/windops-weather", "--replicas=1", namespace=APPLICATION)
    kubectl("rollout", "status", "deployment/windops-weather", "--timeout=180s",
            namespace=APPLICATION)  # fmt: skip


class Callers:
    """A caller for each service, each through its own port-forward, opened when first needed."""

    def __init__(self, key: str, bundle: Any) -> None:
        self.key, self.bundle = key, bundle
        self.stack: list[Any] = []
        self.stubs = {
            "asset": asset_pb2_grpc.AssetServiceStub,
            "investigation": investigation_pb2_grpc.InvestigationServiceStub,
            "policy": policy_pb2_grpc.PolicyServiceStub,
            "production-impact": production_impact_pb2_grpc.ProductionImpactServiceStub,
            "weather": weather_pb2_grpc.WeatherServiceStub,
        }
        self.callers: dict[str, Caller] = {}

    def __getitem__(self, name: str) -> Caller:
        if name not in self.callers:
            forward = port_forward(f"deployment/windops-{name}", APPLICATION, 50051)
            port = forward.__enter__()
            channel_context = blocking_channel(port, HOSTS[name], self.bundle)
            channel = channel_context.__enter__()
            self.stack += [forward, channel_context]
            self.callers[name] = Caller(self.stubs[name](channel), self.key)
        return self.callers[name]

    def forget(self, name: str) -> None:
        """Drop a port-forward to a pod that is gone, so the next call opens a new one."""
        self.callers.pop(name, None)

    def close(self) -> None:
        for context in reversed(self.stack):
            # Closing is best effort: the pod behind a port-forward may be gone.
            with contextlib.suppress(Exception):
                context.__exit__(None, None, None)


def make_turbine(callers: Callers) -> str:
    assets = callers["asset"]
    try:
        assets.call("CreateWindFarm", asset_pb2.CreateWindFarmRequest(
            wind_farm_id=FARM, name="Weather gate", country="GB", time_zone="Europe/London",
            location=asset_pb2.Location(latitude=LATITUDE, longitude=LONGITUDE), offshore=True,
        ), ADMIN, TENANT)  # fmt: skip
    except grpc.RpcError as error:
        # It exists from an earlier run.
        if error.code() not in (grpc.StatusCode.ALREADY_EXISTS,
                                grpc.StatusCode.FAILED_PRECONDITION):  # fmt: skip
            raise
    assets.call("CreateTurbine", asset_pb2.CreateTurbineRequest(
        turbine_id=TURBINE, wind_farm_id=FARM, name="Weather gate T01",
        location=asset_pb2.Location(latitude=LATITUDE, longitude=LONGITUDE),
        model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic", model="WS-8.0-167",
                                     rated_power_kw=8000, rotor_diameter_m=167,
                                     hub_height_m=109),
    ), ADMIN, TENANT)  # fmt: skip
    request = investigation_pb2.CreateCaseRequest(turbine_id=TURBINE, title=TITLE, severity="high")
    return str(callers["investigation"].call("CreateCase", request, OPERATOR, TENANT).case.case_id)


def climb(callers: Callers, case_id: str, tenant: str = TENANT) -> Any:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    proposal = policy_pb2.Proposal(action="inspection", requires_climb=True, confidence=0.9,
                                   planned_start=stamp(start),
                                   planned_end=stamp(start + timedelta(hours=4)))  # fmt: skip
    request = policy_pb2.EvaluateProposalRequest(case_id=case_id, proposal=proposal)
    result = callers["policy"].call("EvaluateProposal", request, ENGINEER, tenant).result
    return next(r for r in result.rules if r.rule_id == "work-at-height-wind"), result


def climb_once_reachable(callers: Callers, case_id: str) -> tuple[Any, Any]:
    """A climb's judgement once Policy reaches the Weather Service again.

    A replaced pod takes a few seconds to take calls, and Policy's breaker for it lets a probe
    through 30 seconds after opening. Until then the climb blocks, as it should.
    """
    wind, result = climb(callers, case_id)
    for _ in range(18):
        if "Weather Service" not in wind.reason:
            break
        time.sleep(5)
        wind, result = climb(callers, case_id)
    return wind, result


def forecast(callers: Callers) -> Any:
    request = weather_pb2.GetForecastRequest(turbine_id=TURBINE)
    return callers["weather"].call("GetForecast", request, OPERATOR, TENANT)


def keep_stale_forecast() -> None:
    """A calm forecast for the site, kept 4 hours ago, as if retrieved then."""
    retrieved = datetime.now(UTC) - timedelta(hours=4)
    base = retrieved.replace(minute=0, second=0, microsecond=0)
    hours = [{"at": (base + timedelta(hours=i)).isoformat(), "wind_speed_10m": 4.0,
              "wind_speed_80m": 5.0, "wind_speed_120m": 5.5, "wind_gusts_10m": 6.0}
             for i in range(24)]  # fmt: skip
    body = json.dumps(hours, sort_keys=True, separators=(",", ":")).replace("'", "''")
    sql("windops_weather", (
        "INSERT INTO forecasts (tenant_id, forecast_id, provider, model, revision, latitude, "  # noqa: S608  # Constants and generated values only.
        "longitude, elevation_m, retrieved_at, valid_from, valid_to, hours, content_hash) VALUES "
        f"('{TENANT}', 'fct_{secrets.token_hex(10)}', 'open-meteo', 'best_match', '', "
        f"{LATITUDE}, {LONGITUDE}, 0, '{retrieved.isoformat()}', '{base.isoformat()}', "
        f"'{(base + timedelta(hours=23)).isoformat()}', '{body}', '{'0' * 64}'); SELECT 1"
    ))  # fmt: skip


def check_provider_unreachable(callers: Callers, case_id: str) -> None:
    provider_closed(True)
    callers.forget("weather")
    answer = forecast(callers)
    check(answer.state == "unavailable" and not answer.HasField("forecast")
          and "cannot be reached" in answer.note,
          f"With the provider unreachable and nothing kept, the forecast is unavailable: "
          f"{answer.note}")  # fmt: skip
    wind, _ = climb(callers, case_id)
    check(wind.outcome == "block" and "unavailable" in wind.reason,
          f"and the climb blocks: {wind.reason}")  # fmt: skip


def check_stale(callers: Callers, case_id: str) -> None:
    keep_stale_forecast()
    answer = forecast(callers)
    check(answer.state == "stale" and answer.age_seconds >= 4 * 3600 - 60
          and answer.summary.max_wind_speed_hub_mps < 12,
          "With only a 4-hour-old calm forecast kept, it is stale, never current: "
          f"{answer.note}")  # fmt: skip
    wind, _ = climb(callers, case_id)
    check(wind.outcome == "block" and "stale" in wind.reason,
          f"and the climb blocks, though that forecast's wind is calm: {wind.reason}")  # fmt: skip


def check_provider_back(callers: Callers, case_id: str) -> None:
    provider_closed(False)
    callers.forget("weather")
    answer = forecast(callers)
    check(answer.state == "fresh" and answer.forecast.provider == "open-meteo",
          f"With the provider back, the forecast is fresh, {answer.age_seconds} s old")  # fmt: skip
    wind, result = climb_once_reachable(callers, case_id)
    given = {i.name: i for i in result.inputs}["weather.max_wind_speed_mps"]
    check(bool(given.value_json), f"Policy has the forecast's hub wind: {wind.reason}")
    hub = json.loads(given.value_json)
    check(wind.outcome == ("block" if hub > 12 else "pass"),
          f"and the climb is judged on it: {hub} m/s at the hub, so {wind.outcome}")  # fmt: skip


def check_service_down(callers: Callers, case_id: str, demo_case: str) -> None:
    service_down(True)
    callers.forget("weather")
    for tenant, case in ((TENANT, case_id), (DEMO_TENANT, demo_case)):
        wind, _ = climb(callers, case, tenant)
        check(wind.outcome == "block" and "Weather Service" in wind.reason,
              f"With the Weather Service down, a climb in {tenant} blocks: "
              f"{wind.reason}")  # fmt: skip
    start = datetime.now(UTC) + timedelta(hours=2)
    estimate = callers["production-impact"].call("EstimateImpact", (
        production_impact_pb2.EstimateImpactRequest(case_id=demo_case, action="repair",
                                                    downtime_hours=12, planned_start=stamp(start))
    ), ENGINEER, DEMO_TENANT).result  # fmt: skip
    held = [a for a in estimate.assumptions if "stays as observed" in a]
    check(
        bool(held) and "gave no forecast" in held[0],
        f"and Production Impact holds the observed wind, saying why: {held[0] if held else ''}",
    )


def check_service_back(callers: Callers, case_id: str) -> None:
    service_down(False)
    callers.forget("weather")
    wind, _ = climb_once_reachable(callers, case_id)
    check("Weather Service" not in wind.reason,
          f"With the Weather Service back, climbs are judged on its forecast again: "
          f"{wind.outcome}")  # fmt: skip


def clean_up() -> None:
    sql("windops_weather", "ALTER TABLE forecasts DISABLE TRIGGER forecasts_never_change; "
        "DELETE FROM forecasts; ALTER TABLE forecasts ENABLE TRIGGER forecasts_never_change; "
        "SELECT 1")  # fmt: skip
    for tenant in (TENANT, DEMO_TENANT):
        sql("windops_policy", "ALTER TABLE policy_results DISABLE TRIGGER "
            "policy_results_never_change; DELETE FROM policy_results WHERE evaluated_by LIKE "
            "'weather-gate-%'; ALTER TABLE policy_results ENABLE TRIGGER "
            "policy_results_never_change; SELECT 1", tenant)  # fmt: skip
        sql("windops_production_impact", "ALTER TABLE impact_results DISABLE TRIGGER "
            "impact_results_never_change; DELETE FROM impact_results WHERE estimated_by LIKE "
            "'weather-gate-%'; ALTER TABLE impact_results ENABLE TRIGGER "
            "impact_results_never_change; SELECT 1", tenant)  # fmt: skip
    asset.sql(f"SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608  # Constants only.
              "DELETE FROM turbines WHERE turbine_id LIKE 'weather-gate-%'; SELECT 1")  # fmt: skip
    cases.clean_up()


def main() -> int:
    callers: Callers | None = None
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        for tenant in (TENANT, DEMO_TENANT):
            bootstrap(tenant, ENGINEER, "reliability-engineer")
            bootstrap(tenant, OPERATOR, "control-room-operator")
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        callers = Callers(key, bundle)
        case_id = make_turbine(callers)
        demo = investigation_pb2.CreateCaseRequest(turbine_id=DEMO_TURBINE, title=TITLE,
                                                   severity="high")  # fmt: skip
        demo_case = callers["investigation"].call("CreateCase", demo, OPERATOR,
                                                  DEMO_TENANT).case.case_id  # fmt: skip
        if not re.fullmatch(r"case_[a-z0-9]+", demo_case):
            raise RuntimeError(f"Unexpected case ID: {demo_case!r}")
        check_provider_unreachable(callers, case_id)
        check_stale(callers, case_id)
        check_provider_back(callers, case_id)
        check_service_down(callers, case_id, demo_case)
        check_service_back(callers, case_id)
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        restore()
        if callers is not None:
            callers.close()
        clean_up()
    print("Weather outage check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
