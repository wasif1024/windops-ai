#!/usr/bin/env python3
"""Prove the simulator streams the whole demo fleet, live, through the gateway (M4.4).

Run `make images`, `make services`, `make gateway-routes`, `make asset-seed` and
`make telemetry-devices` first. It reads what the simulator says it sent, what the Telemetry
Service stored, and what Prometheus scraped, then restarts the simulator to prove a restart loses
nothing and repeats no sequence. Safe to repeat.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.simulator import SOURCE

from scripts.checks.fleet.telemetry import kubectl
from scripts.setup.gateway_routes import port_forward

APPLICATION, OBSERVABILITY = "windops-application", "windops-observability"
DEPLOYMENT = "deployment/windops-simulator"
EVERYONE = "SELECT set_config('windops.all_tenants', 'on', false); "
TURBINES = {(spec.tenant_id, spec.turbine_id) for spec in DEMO_TURBINES}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def wait_until(description: str, condition: Callable[[], bool], seconds: int = 120) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting until {description}")


def rows(query: str) -> list[list[str]]:
    """Run SQL across every tenant, as the Telemetry Service's own login, and split the rows."""
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", "--field-separator=|", "--username=windops_telemetry",
        "--dbname=windops_telemetry", "--set=ON_ERROR_STOP=1", f"--command={EVERYONE}{query}",
        namespace="windops-data",
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    return [line.split("|") for line in result.stdout.strip().splitlines()[1:] if line]


def metrics() -> dict[str, float]:
    with port_forward(DEPLOYMENT, APPLICATION, 8080) as port:
        text = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=10).text
    values = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            values[name] = float(value)
    return values


def labelled(name: str, tenant: str, turbine: str) -> str:
    return f'{name}{{tenant_id="{tenant}",turbine_id="{turbine}"}}'


def check_running() -> None:
    ready = kubectl(
        "get", DEPLOYMENT, "--output=jsonpath={.status.readyReplicas}/{.spec.replicas}"
    ).stdout
    check(ready == "1/1", "The simulator runs as one replica, so nothing is sent twice")


def fresh_turbines() -> set[tuple[str, str]]:
    found = rows(
        "SELECT tenant_id, turbine_id FROM operational_samples "  # noqa: S608  # Constants only, never input.
        f"WHERE source = '{SOURCE}' AND ingested_at > now() - interval '1 minute' "
        "GROUP BY tenant_id, turbine_id"
    )
    return {(tenant, turbine) for tenant, turbine in found}


def check_every_turbine_is_live() -> None:
    wait_until(
        "every demo turbine has sent in the last minute", lambda: fresh_turbines() >= TURBINES
    )
    check(True, f"All {len(TURBINES)} demo turbines sent samples in the last minute")
    values = metrics()
    stale = [
        turbine
        for tenant, turbine in TURBINES
        if time.time() - values.get(labelled(
            "windops_simulator_last_accepted_timestamp_seconds", tenant, turbine), 0) > 30
    ]  # fmt: skip
    check(not stale, "Each device's last batch was accepted in the last 30 seconds")
    refused = sum(v for k, v in values.items() if 'reason="refused_' in k)
    limited = values.get('windops_simulator_requests_total{status="429"}', 0)
    check(refused == 0 and limited == 0, "No batch was refused, and no device hit its rate limit")


def check_freshness() -> None:
    # Delayed deliveries are late on purpose, so look at the median, as a dashboard would.
    lag = rows(
        "SELECT percentile_cont(0.5) WITHIN GROUP "  # noqa: S608  # Constants only, never input.
        "(ORDER BY extract(epoch FROM ingested_at - event_time)) FROM operational_samples "
        f"WHERE source = '{SOURCE}' AND ingested_at > now() - interval '2 minutes'"
    )
    seconds = float(lag[0][0])
    check(seconds < 15, f"Samples arrive {seconds:.1f} s after they are measured, at the median")


def check_prometheus() -> None:
    with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as port:

        def scraped() -> bool:
            answer: Any = httpx.get(
                f"http://127.0.0.1:{port}/api/v1/query",
                params={"query": "count(windops_simulator_samples_accepted_total > 0)"},
                timeout=15,
            ).json()
            result = answer["data"]["result"]
            return bool(result) and int(float(result[0]["value"][1])) == len(TURBINES)

        wait_until("Prometheus has every device's counters", scraped, seconds=180)
    check(True, "Prometheus scrapes each device's counters, for the freshness dashboard (M4.9)")


def latest_sequences() -> dict[tuple[str, str], int]:
    found = rows(
        "SELECT tenant_id, turbine_id, max(sequence) FROM operational_samples "  # noqa: S608  # Constants only, never input.
        f"WHERE source = '{SOURCE}' GROUP BY tenant_id, turbine_id"
    )
    return {(tenant, turbine): int(value) for tenant, turbine, value in found}


def check_restart() -> None:
    before = latest_sequences()
    started = int(time.time())
    kubectl("rollout", "restart", DEPLOYMENT)
    done = kubectl("rollout", "status", DEPLOYMENT, "--timeout=180s")
    check(done.returncode == 0, "The simulator restarts")
    wait_until(
        "every turbine sends again after the restart",
        lambda: all(latest_sequences().get(key, 0) > max(before.get(key, 0), started)
                    for key in TURBINES),
    )  # fmt: skip
    check(True, "After a restart, every turbine's sequence keeps rising")
    repeats = rows(
        "SELECT count(*) FROM (SELECT tenant_id, turbine_id, sequence FROM operational_samples "  # noqa: S608  # Constants only, never input.
        f"WHERE source = '{SOURCE}' AND event_time > to_timestamp({started - 120}) "
        "GROUP BY 1, 2, 3 HAVING count(DISTINCT (power_kw, wind_speed_mps)) > 1) AS twice"
    )
    # The link's injected duplicates are exact copies. Two different samples with one sequence
    # would mean the old and new simulator both made it.
    check(repeats[0][0] == "0", "and no sequence was used for two different samples")


def check_rejections() -> None:
    found = rows(
        "SELECT reason, count(*) FROM ingest_rejections "
        "WHERE received_at > now() - interval '1 hour' GROUP BY reason ORDER BY 2 DESC"
    )
    summary = ", ".join(f"{reason} {count}" for reason, count in found) or "none yet"
    print(f"info - Refused samples in the last hour, by reason: {summary}")


def main() -> int:
    try:
        check_running()
        check_every_turbine_is_live()
        check_freshness()
        check_prometheus()
        check_restart()
        check_rejections()
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Simulator check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
