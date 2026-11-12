#!/usr/bin/env python3
"""Prove a seeded fault profile streams into storage with the correct quality labels (M4.10).

The Phase 4 exit gate (SRS section 12). A check-only turbine, `gate-check-t01` in the tenant
`telemetry-check`, runs a seeded scenario in real time: the gearbox oil overheating profile,
starting a minute in, over a poor link that loses, repeats, delays, breaks and reorders samples.
Its device sends every delivery through the gateway when the simulator says it is sent, so the
delayed ones really arrive late.

Once its periods close, the stored data is compared with the simulator's hidden ground truth:
every injected problem has its label or gap, nothing else does, every reading stored is the one
measured, and the fault's rising oil temperature is there to see. Takes 12 to 22 minutes, as labels
are written once a 10-minute period closes. It reaches the gateway on a local port of its own,
not Keycloak's fixed 9443, so it can run alongside checks that sign in. It removes what it made.
"""

import dataclasses
import ssl
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from windops_simulator.delivery import DataProblems, Delivery
from windops_simulator.scenarios import SCENARIOS, GroundTruth, Scenario, run

from scripts.checks.fleet.data_quality import as_tenant, check, device_token
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import authority_file
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import GATEWAY, NAMESPACE

TENANT, TURBINE = "telemetry-check", "gate-check-t01"
# A device sends what it has every few seconds.
BATCH = timedelta(seconds=5)
PERIOD = timedelta(minutes=10)
# The summarizer's wait after a period ends, and a little more.
GRACE = timedelta(seconds=65)
# A device tries a failed send again, as a real one would, briefly: a batch retried later than
# this would make its samples delayed, and the comparison with the ground truth would fail.
RETRY_SECONDS = (1.0, 2.0, 4.0)
# Each oil temperature over the first minute, before the fault, and the last two, at full heat.
WARMER_BY_C = 2.0


def scenario(start: datetime) -> Scenario:
    """The gearbox fault, sped up to show within minutes, over a link with every problem.

    Seeded, so the problems fall on the same sequences whenever it runs. Delays are 30 to 90
    seconds: past the 20 seconds that makes a sample delayed, and short enough to wait for.
    """
    base = SCENARIOS["gearbox-temp-001"]
    spec = dataclasses.replace(base.turbines[0], tenant_id=TENANT, turbine_id=TURBINE)
    fault = dataclasses.replace(
        base.faults[0], turbine_id=TURBINE,
        onset_after=timedelta(minutes=1), ramp=timedelta(minutes=3),
    )  # fmt: skip
    return dataclasses.replace(
        base,
        scenario_id="phase-4-gate",
        description="Gearbox oil overheating over a poor link, for the Phase 4 exit gate",
        start=start,
        duration=timedelta(minutes=10),
        turbines=(spec,),
        faults=(fault,),
        data_problems=DataProblems(
            missing=0.01, duplicated=0.01, delayed=0.01, malformed=0.01, out_of_order=0.01,
            delay=(timedelta(seconds=30), timedelta(seconds=90)),
        ),
        detectable_after=timedelta(minutes=3),
    )  # fmt: skip


def batches(deliveries: Iterable[Delivery], start: datetime) -> list[tuple[datetime, list[Any]]]:
    """Each delivery in the batch its device sends next, at the end of each BATCH."""
    grouped: dict[datetime, list[Any]] = defaultdict(list)
    for delivery in deliveries:
        seconds = (delivery.sent_at - start).total_seconds()
        grouped[start + BATCH * (int(seconds // BATCH.total_seconds()) + 1)].append(
            delivery.payload
        )
    return sorted(grouped.items(), key=lambda item: item[0])


@dataclass
class Stored:
    """What the Telemetry Service kept, as the gate reads it back."""

    # (sequence, label, gearbox oil temperature), in arrival order.
    rows: list[tuple[int, str, float | None]] = field(default_factory=list)
    gaps: list[tuple[int, int]] = field(default_factory=list)
    # Each rejection's sequence, or None when it could not be read.
    rejections: list[int | None] = field(default_factory=list)
    # Summed over the turbine's 10-minute summaries.
    missing: int = 0
    duplicates: int = 0
    delayed: int = 0


def compare(truth: GroundTruth, sent: list[Any], stored: Stored) -> list[tuple[bool, str]]:
    """Each thing the gate requires, and whether it holds. The same rules as the unit test."""
    injected: dict[str, set[int]] = defaultdict(set)
    for problem in truth.problems:
        injected[problem.kind].add(problem.sequence)
    labels: dict[str, set[int]] = defaultdict(set)
    for sequence, label, _ in stored.rows:
        labels[label].add(sequence)
    gaps = {s for first, last in stored.gaps for s in range(first, last + 1)}
    cut_off = {p.sequence for p in truth.problems
               if p.kind == "malformed" and p.detail == "not_json"}  # fmt: skip
    readable = {p.sequence for p in truth.problems
                if p.kind == "malformed" and p.detail != "not_json"}  # fmt: skip
    unseen = injected["missing"] | injected["malformed"] | injected["delayed"]
    unexplained = {s for s in injected["out_of_order"] - labels["out_of_order"]
                   if s + 1 not in unseen}  # fmt: skip
    measured = {d["sequence"]: d.get("gearbox_oil_temp_c") for d in sent
                if isinstance(d, dict) and "sequence" in d}  # fmt: skip
    oil = [(s, value) for s, label, value in stored.rows if label != "duplicate" and value]
    end = max((s for s, _ in oil), default=0)
    # One sample a second, numbered from 1: the first minute, and the last two.
    first = [value for s, value in oil if s <= 60]
    last = [value for s, value in oil if s > end - 120]
    before = sum(first) / len(first) if first else 0.0
    after = sum(last) / len(last) if last else 0.0
    counts = {kind: len(found) for kind, found in sorted(injected.items())}
    well_formed = len(sent) - len(injected["malformed"])
    return [
        (len(injected) == 5, f"The ground truth has every kind of problem: {counts}"),
        ("received" not in labels, "Every stored sample has a label"),
        (len(stored.rows) == well_formed,
         f"Every well-formed delivery was stored once: {len(stored.rows)} samples"),
        (labels["duplicate"] == injected["duplicated"],
         f"The {len(injected['duplicated'])} samples sent twice: each copy is a duplicate"),
        (labels["delayed"] == injected["delayed"],
         f"The {len(injected['delayed'])} samples held back 30 to 90 seconds are delayed"),
        (gaps == injected["missing"] | cut_off,
         f"The {len(injected['missing'])} samples never sent, and the {len(cut_off)} cut off part "
         "way, are gaps, and nothing else is"),
        (labels["out_of_order"] <= injected["out_of_order"] and not unexplained,
         f"{len(labels['out_of_order'])} of {len(injected['out_of_order'])} samples sent after the "
         "next are out_of_order; the rest followed a sample that never arrived in time"),
        ({s for s in stored.rejections if s is not None} == readable
         and len(stored.rejections) == len(injected["malformed"]),
         f"The {len(injected['malformed'])} broken samples are refused, and kept by sequence where "
         "it could be read"),
        (stored.missing == len(gaps) and stored.duplicates == len(labels["duplicate"])
         and stored.delayed == len(labels["delayed"]),
         "The 10-minute summaries count the same gaps, duplicates and delays"),
        (all(value == measured.get(s) for s, _, value in stored.rows),
         "Every stored reading is the one the simulator measured"),
        (after - before >= WARMER_BY_C,
         f"The fault reached storage: gearbox oil {before:.1f} °C before it, {after:.1f} °C at "
         "full heat"),
    ]  # fmt: skip


def send(client: httpx.Client, url: str, token: str, items: list[Any]) -> dict[str, Any]:
    """One batch through the gateway, retried briefly if the service was not reachable."""
    for pause in (*RETRY_SECONDS, None):
        try:
            answer = client.post(url, json={"samples": items}, timeout=15,
                                 headers={"Authorization": f"Bearer {token}"})  # fmt: skip
        except httpx.TransportError:
            if pause is None:
                raise
        else:
            if answer.status_code == 200:
                result: dict[str, Any] = answer.json()
                return result
            # A refusal is final. Only the gateway or the service being unreachable is retried.
            if answer.status_code not in (502, 503, 504) or pause is None:
                raise RuntimeError(
                    f"The device route answered {answer.status_code}: {answer.text[:200]}"
                )
        print(f"info - the device route was unreachable; trying again in {pause:.0f} s", flush=True)
        time.sleep(pause)
    raise RuntimeError("unreachable")


def stream(token: str, planned: list[tuple[datetime, list[Any]]]) -> None:
    """Send each batch when it is due, as the device would, through the gateway.

    The gateway's certificate names localhost, so any local port will do.
    """
    started = time.monotonic()
    refused = 0
    context = ssl.create_default_context(cafile=authority_file())
    with (
        port_forward(GATEWAY, NAMESPACE, 443) as port,
        httpx.Client(verify=context) as client,
    ):
        url = f"https://localhost:{port}/ingest/v1/samples"
        for number, (due, items) in enumerate(planned, start=1):
            time.sleep(max(0.0, (due - datetime.now(UTC)).total_seconds()))
            answer = send(client, url, token, items)
            refused += len(answer["rejected"])
            if number % 24 == 0:
                print(f"info - {int(time.monotonic() - started) // 60} minutes sent, {refused} "
                      "refused so far", flush=True)  # fmt: skip


def wait_for_labels(last_event: datetime) -> None:
    """Until the last period closes, then summarize, as the service does every minute."""
    seconds = PERIOD.total_seconds()
    closes = datetime.fromtimestamp((last_event.timestamp() // seconds + 1) * seconds, UTC)
    ready = closes + GRACE
    print(f"info - waiting until {ready:%H:%M:%S} for the last period to close", flush=True)
    time.sleep(max(0.0, (ready - datetime.now(UTC)).total_seconds()))
    for _ in range(12):
        done = kubectl("exec", "deployment/windops-telemetry", "--", "python", "-m",
                       "windops_telemetry.upkeep", "--summaries")  # fmt: skip
        if done.returncode != 0:
            raise RuntimeError(f"Upkeep failed: {done.stderr.strip()}")
        if as_tenant(f"SELECT count(*) FROM operational_samples WHERE {MINE} "  # noqa: S608
                     "AND quality_status = 'received'") == "0":  # fmt: skip
            return
        time.sleep(10)


# The check's turbine. Constants, never input.
MINE = f"tenant_id = '{TENANT}' AND turbine_id = '{TURBINE}'"


def read_back() -> Stored:
    rows = as_tenant(
        "SELECT string_agg(sequence || ':' || quality_status || ':' || "  # noqa: S608
        "coalesce(gearbox_oil_temp_c::text, ''), ',' ORDER BY ingested_at, arrival_index) "
        f"FROM operational_samples WHERE {MINE}"
    )
    gaps = as_tenant(
        "SELECT string_agg(first_sequence || '-' || last_sequence, ',') "  # noqa: S608
        f"FROM data_gaps WHERE {MINE}"
    )
    rejected = as_tenant(
        "SELECT string_agg(coalesce(sequence::text, '-'), ',') "  # noqa: S608
        f"FROM ingest_rejections WHERE {MINE}"
    )
    sums = as_tenant(
        "SELECT coalesce(sum(missing_samples), 0) || '|' || coalesce(sum(duplicate_samples), 0) "  # noqa: S608
        f"|| '|' || coalesce(sum(delayed_samples), 0) FROM scada_summaries WHERE {MINE}"
    )
    stored = Stored()
    for item in filter(None, rows.split(",")):
        sequence, label, oil = item.split(":")
        stored.rows.append((int(sequence), label, float(oil) if oil else None))
    for item in filter(None, gaps.split(",")):
        first, last = item.split("-")
        stored.gaps.append((int(first), int(last)))
    stored.rejections = [None if s == "-" else int(s) for s in filter(None, rejected.split(","))]
    stored.missing, stored.duplicates, stored.delayed = (int(n) for n in sums.split("|"))
    return stored


def clean_up() -> None:
    statements = [
        f"DELETE FROM {table} WHERE {MINE}"  # noqa: S608  # Constants only, never input.
        for table in ("operational_samples", "scada_summaries", "data_gaps", "ingest_rejections")
    ]
    as_tenant("; ".join(statements) + "; SELECT 1")
    revoke()


def revoke() -> None:
    """Once it has sent everything, so the freshness alerts do not wait for it."""
    as_tenant(f"UPDATE device_credentials SET revoked_at = now() WHERE {MINE} "  # noqa: S608
              "AND revoked_at IS NULL; SELECT 1")  # fmt: skip


def main() -> int:
    try:
        clean_up()
        token = device_token(TENANT, TURBINE)
        start = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=5)
        planned_scenario = scenario(start)
        deliveries, truth = run(planned_scenario)
        delivered = list(deliveries)
        sent = [d.payload for d in delivered]
        planned = batches(delivered, start)
        print(f"info - streaming {len(sent)} deliveries for {TURBINE}, from {start:%H:%M:%S}, "
              f"until {planned[-1][0]:%H:%M:%S}", flush=True)  # fmt: skip
        stream(token, planned)
        revoke()
        wait_for_labels(start + planned_scenario.duration)
        for passed, message in compare(truth, sent, read_back()):
            check(passed, message)
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Fault stream check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
