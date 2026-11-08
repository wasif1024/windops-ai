#!/usr/bin/env python3
"""Prove CMS vibration on the cluster: live features, a fault's signature and raw windows.

Run with the simulator streaming for at least 11 minutes, so every device has sent a CMS batch.
Needs local port 9443 free. It sends one worn-bearing batch through the gateway, as a demo
turbine's device, with source `cms-check`, and removes it at the end.
"""

import base64
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from windops_simulator.faults import FaultSpec
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.simulator import SOURCE, Simulator

from scripts.checks.fleet.telemetry import kubectl, sql
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import PUBLIC_URL, keycloak_public
from scripts.setup.telemetry_devices import DEVICE_STORE

TENANT, TURBINE = "north-sea", "hornsea-west-t02"
CHECK_SOURCE = "cms-check"
CMS = PUBLIC_URL.removesuffix("/auth") + "/ingest/v1/cms"
EVERYONE = "SELECT set_config('windops.all_tenants', 'on', false); "


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def wait_until(description: str, condition: Callable[[], bool], seconds: int = 720) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(10)
    raise RuntimeError(f"Timed out waiting until {description}")


def token() -> str:
    encoded = kubectl(
        "get", "secret", DEVICE_STORE, f"--output=jsonpath={{.data.{TENANT}\\.{TURBINE}}}"
    ).stdout
    if not encoded:
        raise RuntimeError(f"No token in {DEVICE_STORE}: run make telemetry-devices")
    return base64.b64decode(encoded).decode()


def check_live_features() -> None:
    expected = len(DEMO_TURBINES) * 3

    def recent() -> int:
        found = sql(
            f"{EVERYONE}SELECT count(DISTINCT (tenant_id, turbine_id, sensor)) FROM cms_features "  # noqa: S608
            f"WHERE source = '{SOURCE}' AND ingested_at > now() - interval '11 minutes'"
        )
        return int(found or 0)

    wait_until(f"all {expected} CMS sensors have sent in the last 11 minutes",
               lambda: recent() == expected)  # fmt: skip
    sane = sql(
        f"{EVERYONE}SELECT bool_and(feature_version = '1' AND samples = 8192 AND rms > 0 "  # noqa: S608
        "AND kurtosis > 0), bool_and(sensor <> 'gearbox' OR shaft_hz < 0.05 "
        "OR band_gear_mesh_rms IS NOT NULL) FROM cms_features "
        f"WHERE source = '{SOURCE}' AND ingested_at > now() - interval '11 minutes'"
    )
    check(sane == "t|t", f"All {expected} demo sensors (13 turbines x 3) send CMS windows, and "
          "each has its features, with gear mesh on turning gearboxes (FR-TEL-004)")  # fmt: skip


def worn_batch() -> dict[str, Any]:
    spec = next(spec for spec in DEMO_TURBINES if spec.turbine_id == TURBINE)
    moment = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=90)
    worn = FaultSpec("bearing_wear", TURBINE, timedelta(0), timedelta(seconds=1),
                     component="main_bearing")  # fmt: skip
    simulator = Simulator(4242, (spec,), moment - timedelta(seconds=120), (worn,))
    for _ in range(120):
        simulator.step(1.0)
    batch = simulator.cms_batch(TURBINE)
    return {**batch, "source": CHECK_SOURCE, "trigger": "cms-check"}


def check_fault_signature() -> None:
    sent = worn_batch()
    with keycloak_public():
        answer = httpx.post(CMS, json=sent, headers={"Authorization": f"Bearer {token()}"},
                            timeout=30)  # fmt: skip
    check(
        answer.status_code == 200 and answer.json()["accepted"] == 3,
        "A CMS batch goes through the gateway's device route, and all 3 windows are stored",
    )
    where = f"tenant_id = '{TENANT}' AND turbine_id = '{TURBINE}' AND sensor = 'main_bearing'"
    as_tenant = f"SELECT set_config('windops.tenant_id', '{TENANT}', false); "
    worn = sql(f"{as_tenant}SELECT kurtosis FROM cms_features WHERE {where} "  # noqa: S608
               f"AND source = '{CHECK_SOURCE}'")  # fmt: skip
    healthy = sql(f"{as_tenant}SELECT avg(kurtosis) FROM cms_features WHERE {where} "  # noqa: S608
                  f"AND source = '{SOURCE}' AND shaft_hz > 0.05")  # fmt: skip
    check(
        float(worn) > 5 * float(healthy),
        f"A worn main bearing's kurtosis, {float(worn):.1f}, stands far above the live healthy "
        f"level of {float(healthy):.1f}",
    )
    raw = sql(
        f"{as_tenant}SELECT w.kept_for, length(w.values_f32le), w.samples "  # noqa: S608
        "FROM cms_waveforms w JOIN cms_features f USING (tenant_id, turbine_id, sensor, "
        f"event_time) WHERE f.sensor = 'main_bearing' AND f.source = '{CHECK_SOURCE}'"
    )
    check(
        raw == f"trigger:cms-check|{8192 * 4}|8192",
        "Its raw window is kept, as 8192 float32 values, because the device flagged it",
    )


def check_prometheus() -> None:
    with port_forward("service/windops-prometheus", "windops-observability", 9090) as port:

        def scraped() -> bool:
            answer: Any = httpx.get(
                f"http://127.0.0.1:{port}/api/v1/query",
                params={"query": "count(windops_simulator_cms_windows_accepted_total > 0)"},
                timeout=15,
            ).json()
            result = answer["data"]["result"]
            return bool(result) and int(float(result[0]["value"][1])) == len(DEMO_TURBINES)

        wait_until("Prometheus counts every device's CMS windows", scraped, seconds=180)
    check(True, "Prometheus counts each device's accepted CMS windows")


def check_partitions() -> None:
    now = datetime.now(UTC)
    names = [f"cms_features_{now:%Y%m}", "cms_features_default", f"cms_waveforms_{now:%Y%m%d}",
             "cms_waveforms_default"]  # fmt: skip
    quoted = ", ".join(f"'{name}'" for name in names)
    forced = sql(
        f"SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND relname IN ({quoted})"  # noqa: S608
    )
    check(forced == "4", "CMS features (by month) and raw windows (by day) have partitions, "
          "each with forced row-level security")  # fmt: skip


def clean_up() -> None:
    sql(
        f"SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608
        "DELETE FROM cms_waveforms w USING cms_features f WHERE w.tenant_id = f.tenant_id "
        "AND w.turbine_id = f.turbine_id AND w.sensor = f.sensor AND w.event_time = f.event_time "
        f"AND f.source = '{CHECK_SOURCE}'; "
        f"DELETE FROM cms_features WHERE source = '{CHECK_SOURCE}'; SELECT 1"
    )


def main() -> int:
    try:
        check_partitions()
        check_live_features()
        check_fault_signature()
        check_prometheus()
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("CMS check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
