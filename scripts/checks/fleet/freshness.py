#!/usr/bin/env python3
"""Prove the Data Freshness dashboard and its alerts on the cluster, against the live fleet (M4.9).

Run the platform and the simulator first, as `make dev` does. Needs local port 9443 free. It
proves what SRS 10.3 asks of telemetry delay and stale data:

- every demo turbine is live, and its samples arrive within seconds;
- a sample the device route refuses is counted by reason, for failed ingestion;
- a turbine whose newest sample is ten minutes old is stale, and raises WindopsTelemetryStale;
- a turbine with a device credential that never sent anything is counted as silent;
- every panel's query has data, and Grafana has the dashboard.

The stale and silent turbines are check-only: a device credential that cannot sign in, and one
old sample, written straight to the database, so the simulator streams on undisturbed. They are
removed at the end. Takes about four minutes, as the stale alert waits two minutes to fire.
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from windops_simulator.fleet import DEMO_TURBINES

from scripts.checks.fleet.telemetry import check, device_token, kubectl, samples, send, sql
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

ROOT = Path(__file__).resolve().parents[3]
DASHBOARDS = ROOT / "infrastructure" / "monitoring" / "grafana" / "dashboards"
DASHBOARD = DASHBOARDS / "data-freshness.json"
OBSERVABILITY = "windops-observability"
TENANT, TURBINE, OTHER = "north-sea", "dogger-east-t01", "hornsea-west-t01"
# Check-only turbines, with no assets and no simulator behind them.
STALE, SILENT = "freshness-check-stale", "freshness-check-silent"
# The fleet the simulator streams, which must always be live.
DEMO = {spec.turbine_id for spec in DEMO_TURBINES}
ALERTS = {
    "WindopsTelemetryStale", "WindopsTelemetrySilent",
    "WindopsTelemetryDelayed", "WindopsTelemetryRejected",
}  # fmt: skip
# Each turbine's newest sample is read every 15 s, scraped every 30 s.
SETTLE_SECONDS = 90
# The refused sample's sequence, unique to this run, so its rejection can be removed.
SEQUENCE = int(time.time() * 1000)


class Prometheus:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def query(self, expression: str) -> list[dict[str, Any]]:
        answer = httpx.get(f"{self.base}/api/v1/query", params={"query": expression}, timeout=20)
        body = answer.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus refused the query: {expression}")
        result: list[dict[str, Any]] = body["data"]["result"]
        return result

    def values(self, expression: str, label: str) -> dict[str, float]:
        return {
            item["metric"].get(label, ""): float(item["value"][1])
            for item in self.query(expression)
        }

    def wait_for(self, expression: str, label: str, ready: Any) -> dict[str, float]:
        found: dict[str, float] = {}
        deadline = time.monotonic() + SETTLE_SECONDS
        while time.monotonic() < deadline:
            found = self.values(expression, label)
            if ready(found):
                break
            time.sleep(5)
        return found

    def alerts(self) -> list[dict[str, Any]]:
        answer = httpx.get(f"{self.base}/api/v1/alerts", timeout=20)
        alerts: list[dict[str, Any]] = answer.json()["data"]["alerts"]
        return alerts


def add_check_turbines() -> None:
    """A stale turbine, whose one sample is ten minutes old, and a silent one, with none."""
    devices = ", ".join(
        f"('{TENANT}', '{name}', '{name}', 'not-a-usable-hash', 'freshness-check', now())"
        for name in (STALE, SILENT)
    )
    sql(
        f"SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608
        "INSERT INTO device_credentials (tenant_id, device_id, turbine_id, secret_hash, "
        f"created_by, created_at) VALUES {devices}; "
        "INSERT INTO operational_samples (tenant_id, turbine_id, event_time, sequence, device_id, "
        "ingested_at, source, schema_version, model_version, synthetic, operating_state, "
        f"available, quality_status) VALUES ('{TENANT}', '{STALE}', now() - interval '10 minutes', "
        f"1, '{STALE}', now() - interval '10 minutes', 'freshness-check', '1', '1', true, "
        "'producing', true, 'received')"
    )  # fmt: skip


# The check's own rows, by its fixed names. Constants, not input.
CLEAN_UP = (
    f"SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608
    f"DELETE FROM device_credentials WHERE created_by = 'freshness-check'; "
    f"DELETE FROM operational_samples WHERE source = 'freshness-check'; "
    f"DELETE FROM scada_summaries WHERE turbine_id IN ('{STALE}', '{SILENT}'); "
    f"DELETE FROM data_gaps WHERE turbine_id IN ('{STALE}', '{SILENT}'); "
    f"DELETE FROM ingest_rejections WHERE turbine_id = '{TURBINE}' AND sequence = {SEQUENCE}"
)


def check_live(prometheus: Prometheus) -> None:
    """Every demo turbine the simulator streams is live.

    Other checks' own turbines come and go with their device credentials, and may run at the
    same time, as in the Phase 4 gate, so only the demo fleet is required to be live.
    """
    age = "max by (turbine_id) (windops_telemetry_sample_age_seconds)"
    ages = prometheus.wait_for(
        age, "turbine_id",
        lambda found: all(found.get(turbine, 61) <= 60 for turbine in DEMO),
    )  # fmt: skip
    oldest = max((ages.get(turbine, float("inf")) for turbine in DEMO), default=0)
    check(
        oldest <= 60,
        f"All {len(DEMO)} demo turbines are live: newest samples at most {oldest:.0f} s old",
    )
    states = prometheus.values(
        "sum by (state) (max by (tenant_id, state) (windops_telemetry_turbines))", "state"
    )
    check(states.get("live", 0) >= len(DEMO), f"and counted as live, by tenant: {states}")
    p95 = (
        "histogram_quantile(0.95, sum by (le) "
        '(rate(windops_telemetry_ingest_delay_seconds_bucket{kind="samples"}[5m])))'
    )
    delay = prometheus.values(p95, "")
    check(
        0 < delay.get("", 0) <= 30,
        f"Samples are stored {delay.get('', 0):.1f} s after they are measured, at the 95th "
        "percentile",
    )


def check_refused(prometheus: Prometheus) -> None:
    sample = samples(OTHER, 1, SEQUENCE, "freshness-check")[0]
    with keycloak_public():
        answer = send(device_token(TENANT, TURBINE), [sample])
    check(
        answer.status_code == 200 and answer.json()["rejected"][0]["reason"] == "wrong_turbine",
        "A device's sample for another turbine is refused",
    )
    refused = 'sum by (reason) (windops_telemetry_ingest_samples_total{outcome="rejected"})'
    counted = prometheus.wait_for(refused, "reason", lambda found: "wrong_turbine" in found)
    check("wrong_turbine" in counted, "and counted by its reason, for failed ingestion")


def check_stale_and_silent(prometheus: Prometheus) -> None:
    ages = prometheus.wait_for(
        "max by (turbine_id) (windops_telemetry_sample_age_seconds)",
        "turbine_id",
        lambda found: STALE in found,
    )
    check(
        ages.get(STALE, 0) >= 590,
        f"A turbine whose newest sample is ten minutes old reports {ages.get(STALE, 0):.0f} s",
    )
    states = prometheus.values(
        f'max by (state) (windops_telemetry_turbines{{tenant_id="{TENANT}"}})', "state"
    )
    check(
        states.get("stale", 0) >= 1 and states.get("silent", 0) >= 1,
        f"It is stale, and the turbine that never sent is silent: {states}",
    )
    firing: dict[str, str] = {}
    # Two minutes of `for`, and evaluated every 30 seconds.
    for _ in range(36):
        firing = {
            alert["labels"].get("turbine_id", ""): alert["state"]
            for alert in prometheus.alerts()
            if alert["labels"]["alertname"] == "WindopsTelemetryStale"
        }
        if firing.get(STALE) == "firing":
            break
        time.sleep(5)
    check(firing.get(STALE) == "firing", "WindopsTelemetryStale fires for the stale turbine")
    check(not set(firing) & DEMO, "and for no demo turbine")


def check_rules(prometheus: Prometheus) -> None:
    groups = httpx.get(f"{prometheus.base}/api/v1/rules", timeout=20).json()["data"]["groups"]
    loaded = {rule["name"] for group in groups for rule in group["rules"]}
    check(loaded >= ALERTS, "Prometheus has every telemetry freshness alert")


def check_panels(prometheus: Prometheus) -> None:
    board = json.loads(DASHBOARD.read_text())
    for panel in board["panels"]:
        for target in panel.get("targets", []):
            result = prometheus.query(target["expr"])
            check(
                bool(result),
                f"The panel '{panel['title']}' shows data ({target['legendFormat']})",
            )


def check_grafana() -> None:
    encoded = kubectl(
        "get", "secret", "windops-grafana-admin", "--output=jsonpath={.data.admin-password}",
        namespace=OBSERVABILITY,
    ).stdout  # fmt: skip
    password = base64.b64decode(encoded).decode()
    with port_forward("service/windops-grafana", OBSERVABILITY, 3000) as port:
        board = httpx.get(
            f"http://127.0.0.1:{port}/api/dashboards/uid/windops-data-freshness",
            auth=("admin", password),
            timeout=20,
        )
    check(
        board.status_code == 200 and board.json()["dashboard"]["title"] == "Data Freshness",
        "Grafana has the Data Freshness dashboard",
    )


def main() -> int:
    try:
        sql(CLEAN_UP)
        with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as port:
            prometheus = Prometheus(port)
            check_rules(prometheus)
            check_live(prometheus)
            check_refused(prometheus)
            add_check_turbines()
            check_stale_and_silent(prometheus)
            check_panels(prometheus)
        check_grafana()
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        sql(CLEAN_UP)
    print("Freshness check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
