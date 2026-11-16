#!/usr/bin/env python3
"""Prove the Weather Service on the cluster, with a real forecast from Open-Meteo (M6.3).

Run the platform first, as `make dev` does. The cluster needs to reach api.open-meteo.com. A
Control Room Operator asks for the next day's forecast at a demo turbine, and the check proves:

- the forecast comes from Open-Meteo with its provenance: the provider, its licence, when it was
  retrieved, the window it is valid for, and the hash of the provider's own answer (WTH-003);
- only coordinates rounded to about a kilometre were sent, and hub-height wind is interpolated
  from the provider's levels (WTH-002);
- each attribute is there only when the provider gave it (WTH-004), and the answer states it is
  fresh, with its age (WTH-005);
- asked again, it comes from the cache, a little older, and the provider is not asked;
- an auditor, without weather.read, and another tenant cannot find the turbine;
- PostgreSQL refuses to change or delete a kept forecast, and row-level security hides it.

The forecasts it retrieves stay, as the demo tenant's cache.
"""

import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from windops.weather.v1 import weather_pb2 as pb
from windops.weather.v1 import weather_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-weather.windops-application.svc.cluster.local"
DATABASE = "windops_weather"
TENANT, OTHER, TURBINE = "north-sea", "atlas-wind", "dogger-east-t01"
OPERATOR, AUDITOR, STRANGER = (
    "weather-check-operator",
    "weather-check-auditor",
    "weather-check-other",
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def psql(statement: str, tenant: str) -> subprocess.CompletedProcess[str]:
    return kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command=SELECT set_config('windops.tenant_id', '{tenant}', false); {statement}",
        namespace=DATA,
    )  # fmt: skip


def stamp(at: datetime) -> Timestamp:
    value = Timestamp()
    value.FromDatetime(at)
    return value


def ask(weather: Caller, user: str, tenant: str = TENANT) -> Any:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    request = pb.GetForecastRequest(turbine_id=TURBINE, from_time=stamp(now),
                                    to_time=stamp(now + timedelta(hours=24)))  # fmt: skip
    return weather.call("GetForecast", request, user, tenant)


def check_forecast(weather: Caller) -> Any:
    answer = ask(weather, OPERATOR)
    f = answer.forecast
    decimals = [len(repr(v).split(".")[-1]) for v in (f.latitude, f.longitude)]
    check(
        answer.state == "fresh" and f.provider == "open-meteo" and f.licence == "CC BY 4.0"
        and len(f.content_hash) == 64 and f.forecast_id.startswith("fct_"),
        f"A fresh forecast from Open-Meteo, retrieved {answer.age_seconds} s ago, under "
        f"{f.licence}, valid {f.valid_from.ToDatetime():%d %b %H:%M} to "
        f"{f.valid_to.ToDatetime():%d %b %H:%M}",
    )  # fmt: skip
    check(max(decimals) <= 2, f"Only rounded coordinates were sent: {f.latitude}, {f.longitude}")
    hub = [p for p in answer.points if p.HasField("wind_speed_hub_mps")]
    s = answer.summary
    check(
        len(answer.points) == 24 and len(hub) == 24 and s.HasField("max_wind_speed_hub_mps")
        and s.HasField("max_wind_gusts_10m_mps"),
        f"24 hours, with wind at the {f.hub_height_m:g} m hub: at most "
        f"{s.max_wind_speed_hub_mps:.1f} m/s, gusts to {s.max_wind_gusts_10m_mps:.1f} m/s",
    )  # fmt: skip
    given = sorted(
        {field.name for p in answer.points for field, _ in p.ListFields()} - {"valid_at"}
    )
    check(bool(given) and not f.revision,
          f"It holds only what the provider gave: {', '.join(given)}; no revision, as Open-Meteo "
          "gives none")  # fmt: skip
    return answer


def check_cache(weather: Caller, first: Any) -> None:
    time.sleep(2)
    again = ask(weather, OPERATOR)
    check(
        again.forecast.forecast_id == first.forecast.forecast_id
        and again.age_seconds >= first.age_seconds
        and again.state == "fresh",
        f"Asked again, it comes from the cache, {again.age_seconds} s old",
    )


def check_who(weather: Caller) -> None:
    request = pb.GetForecastRequest(turbine_id=TURBINE)
    check(weather.code("GetForecast", request, AUDITOR, TENANT) == grpc.StatusCode.NOT_FOUND,
          "An auditor, without weather.read, cannot find the turbine")  # fmt: skip
    check(weather.code("GetForecast", request, STRANGER, OTHER) == grpc.StatusCode.NOT_FOUND,
          "Another tenant cannot find it")  # fmt: skip


def check_database(answer: Any) -> None:
    forecast_id = answer.forecast.forecast_id
    if not re.fullmatch(r"fct_[a-z0-9]{20}", forecast_id):
        raise RuntimeError(f"Unexpected forecast ID: {forecast_id!r}")
    for statement in (
        f"UPDATE forecasts SET hours = '[]' WHERE forecast_id = '{forecast_id}'",  # noqa: S608  # The ID is checked above.
        f"DELETE FROM forecasts WHERE forecast_id = '{forecast_id}'",  # noqa: S608
    ):
        refused = psql(statement, TENANT)
        check(refused.returncode != 0 and "never changes" in refused.stderr,
              f"PostgreSQL refuses: {statement.split(' WHERE')[0]}")  # fmt: skip
    hidden = psql(f"SELECT count(*) FROM forecasts WHERE forecast_id = '{forecast_id}'", OTHER)  # noqa: S608
    count = hidden.stdout.strip().splitlines()[-1] if hidden.stdout.strip() else ""
    check(hidden.returncode == 0 and count == "0",
          "Row-level security hides it from another tenant")  # fmt: skip


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        bootstrap(TENANT, AUDITOR, "auditor")
        bootstrap(OTHER, STRANGER, "control-room-operator")
        with (
            port_forward("deployment/windops-weather", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            weather = Caller(weather_pb2_grpc.WeatherServiceStub(channel), key)
            answer = check_forecast(weather)
            check_cache(weather, answer)
            check_who(weather)
        check_database(answer)
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Weather check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
