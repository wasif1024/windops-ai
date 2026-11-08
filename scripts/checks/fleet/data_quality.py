#!/usr/bin/env python3
"""Prove data-quality labels on the cluster: one of each problem, sent and labelled (FR-TEL-003).

Needs local port 9443 free, and can take up to 12 minutes, as labels are written once a 10-minute
period has closed. It gives a check-only turbine, `quality-check-t01` in the tenant
`telemetry-check`, a device credential, and sends through the gateway, as a device would:
samples on time, one twice, two swapped, two never sent, one broken, and one held back. Once the
period closes it asks for every label and gap, then checks the live fleet is labelled too. It
removes what it made at the end.
"""

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from windops_simulator.simulator import SOURCE as SIMULATOR

from scripts.checks.fleet.telemetry import kubectl, sql
from scripts.setup.identity_seed import PUBLIC_URL, keycloak_public

TENANT, TURBINE, SOURCE = "telemetry-check", "quality-check-t01", "quality-check"
SAMPLES = PUBLIC_URL.removesuffix("/auth") + "/ingest/v1/samples"
PERIOD = 600


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def as_tenant(statement: str) -> str:
    return sql(f"SELECT set_config('windops.tenant_id', '{TENANT}', false); {statement}")


def device_token() -> str:
    made = kubectl(
        "exec", "deployment/windops-telemetry", "--", "python", "-m", "windops_telemetry.devices",
        "--replace", f"{TENANT}:{TURBINE}",
    )  # fmt: skip
    if made.returncode != 0:
        raise RuntimeError(f"Could not make a device credential: {made.stderr.strip()}")
    return str(json.loads(made.stdout.splitlines()[-1])["token"])


def sample(sequence: int) -> dict[str, Any]:
    return {
        "schema_version": "2", "tenant_id": TENANT, "turbine_id": TURBINE,
        "event_time": datetime.fromtimestamp(sequence, UTC).isoformat(), "sequence": sequence,
        "source": SOURCE, "model_version": "check", "synthetic": True,
        "operating_state": "producing", "available": True, "power_kw": 1000.0,
        **dict.fromkeys(
            ("wind_speed_mps", "wind_direction_deg", "rotor_speed_rpm", "generator_speed_rpm",
             "pitch_angle_deg", "ambient_temp_c", "gearbox_oil_temp_c", "gearbox_bearing_temp_c",
             "main_bearing_temp_c", "generator_bearing_temp_c", "nacelle_vibration_mm_s"),
            1.0,
        ),
    }  # fmt: skip


def send(token: str, batch: list[Any]) -> dict[str, Any]:
    answer = httpx.post(SAMPLES, json={"samples": batch}, timeout=15,
                        headers={"Authorization": f"Bearer {token}"})  # fmt: skip
    if answer.status_code != 200:
        raise RuntimeError(f"The device route answered {answer.status_code}: {answer.text[:200]}")
    result: dict[str, Any] = answer.json()
    return result


def check_each_problem(token: str) -> None:
    # Start half a minute before a period ends, so the period closes soon after.
    now = time.time()
    end = (int(now) // PERIOD + 1) * PERIOD
    if end - now < 35:
        end += PERIOD
    print(
        f"info - waiting until {datetime.fromtimestamp(end - 30, UTC):%H:%M:%S} to send", flush=True
    )
    time.sleep(max(0.0, end - 30 - time.time()))
    last = int(time.time())
    held, twice, swapped, broken = last - 14, last - 10, (last - 7, last - 6), last - 3
    never = {last - 12, last - 2}
    batch: list[Any] = []
    for sequence in range(last - 13, last + 1):
        if sequence in never or sequence == swapped[1]:
            continue
        if sequence == broken:
            batch.append({**sample(sequence), "power_kw": "1000kW"})
            continue
        batch.append(sample(sequence))
        if sequence == swapped[0]:
            batch.append(sample(swapped[1]))
            batch[-2], batch[-1] = batch[-1], batch[-2]
        if sequence == twice:
            batch.append(sample(sequence))
    answer = send(token, batch)
    check(
        [r["reason"] for r in answer["rejected"]] == ["bad_power_kw"],
        "A device's batch is stored, with its broken sample refused for its reason",
    )
    # 25 seconds later, the sample it held back: measured 39 seconds before it arrives.
    time.sleep(25)
    send(token, [sample(held)])
    time.sleep(max(0.0, end + 65 - time.time()))
    done = kubectl("exec", "deployment/windops-telemetry", "--", "python", "-m",
                   "windops_telemetry.upkeep", "--summaries")  # fmt: skip
    if done.returncode != 0:
        raise RuntimeError(f"Upkeep failed: {done.stderr.strip()}")

    rows = as_tenant(
        "SELECT string_agg(sequence || ':' || quality_status, ',' ORDER BY sequence, "  # noqa: S608
        f"ingested_at, arrival_index) FROM operational_samples WHERE source = '{SOURCE}'"
    )
    labels: dict[int, list[str]] = {}
    for item in rows.split(","):
        number, status = item.split(":")
        labels.setdefault(int(number), []).append(status)
    check(labels.get(twice) == ["ok", "duplicate"], "A sample sent twice: the copy is a duplicate")
    check(labels.get(swapped[0]) == ["out_of_order"] and labels.get(swapped[1]) == ["ok"],
          "Two samples sent the wrong way round: the earlier one is out_of_order")  # fmt: skip
    check(labels.get(held) == ["delayed"], "A sample held back 39 seconds is delayed")
    others = [s for s, found in labels.items() if s not in (twice, held, *swapped)]
    check(all(labels[s] == ["ok"] for s in others), f"The other {len(others)} samples are ok")
    gaps = as_tenant(
        "SELECT string_agg(first_sequence || '-' || last_sequence, ',' ORDER BY first_sequence) "  # noqa: S608
        f"FROM data_gaps WHERE tenant_id = '{TENANT}' AND turbine_id = '{TURBINE}'"
    )
    expected = ",".join(f"{n}-{n}" for n in sorted(never))
    check(gaps == expected, "The two samples never sent are recorded as gaps, and the broken one "
          "is not, as it came")  # fmt: skip
    counts = as_tenant(
        "SELECT duplicate_samples, delayed_samples, out_of_order_samples, missing_samples, "  # noqa: S608
        f"malformed_samples FROM scada_summaries WHERE tenant_id = '{TENANT}' "
        f"AND turbine_id = '{TURBINE}'"
    )
    check(counts == "1|1|1|2|1", "Its 10-minute summary counts one of each, and two missing")


def telemetry_started() -> str:
    """When the running Telemetry Service started. Samples from before it may predate labels."""
    found = kubectl(
        "get", "pods", "--selector=app.kubernetes.io/name=windops-telemetry",
        "--output=jsonpath={.items[0].status.startTime}",
    ).stdout.strip()  # fmt: skip
    if not found:
        raise RuntimeError("The Telemetry Service is not running")
    return found


def check_live_fleet() -> None:
    started = telemetry_started()
    unlabelled = sql(
        "SELECT set_config('windops.all_tenants', 'on', false); SELECT count(*) "  # noqa: S608
        f"FROM operational_samples WHERE source = '{SIMULATOR}' AND quality_status = 'received' "
        f"AND event_time < now() - interval '12 minutes' AND ingested_at > '{started}'"
    )
    check(
        unlabelled == "0",
        "Every live sample from a closed period, since the service started, has its label",
    )
    seen = sql(
        "SELECT set_config('windops.all_tenants', 'on', false); SELECT string_agg(quality_status "  # noqa: S608
        "|| ' ' || n, ', ' ORDER BY n DESC) FROM (SELECT quality_status, count(*) n FROM "
        f"operational_samples WHERE source = '{SIMULATOR}' AND ingested_at > now() - "
        "interval '1 hour' GROUP BY 1) labels"
    )
    gaps = sql(
        "SELECT set_config('windops.all_tenants', 'on', false); SELECT coalesce(sum(missing), 0) "
        "FROM data_gaps WHERE detected_at > now() - interval '1 hour'"
    )
    print(f"info - The live fleet's last hour, by label: {seen}; missing in gaps: {gaps}")


def clean_up() -> None:
    mine = f"tenant_id = '{TENANT}' AND turbine_id = '{TURBINE}'"
    statements = [
        f"DELETE FROM {table} WHERE {mine}"  # noqa: S608  # Constants only, never input.
        for table in ("operational_samples", "scada_summaries", "data_gaps", "ingest_rejections")
    ]
    statements.append(
        f"UPDATE device_credentials SET revoked_at = now() WHERE {mine} AND revoked_at IS NULL"  # noqa: S608
    )
    as_tenant("; ".join(statements) + "; SELECT 1")


def main() -> int:
    try:
        clean_up()
        token = device_token()
        with keycloak_public():
            check_each_problem(token)
        check_live_fleet()
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Data-quality check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
