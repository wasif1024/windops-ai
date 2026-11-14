#!/usr/bin/env python3
"""Prove a fault creates one deduplicated case bound to an immutable snapshot (M5.7).

The Phase 5 exit gate (SRS section 12). A check-only turbine of its own in the tenant
`telemetry-check`, with the demo turbine's model, runs the seeded gearbox oil overheating scenario
through the gateway's device route, as a device would. Nothing is tuned for it: the tenant's
default rule library, the detector and the case rules are the ones every tenant has.

The normal behaviour models need two thermal time constants of history before they judge, so
the device first sends the hour before the fault, as a turbine that has been streaming would
have. The detector is held back until that hour is in, so it reads it whole. Then the fault
streams live, and the check proves:

- nothing is found before the fault, and the default rules find it on their own: the gearbox oil
  and gearbox bearing models, at rising severity;
- every detection, from both rules and each change of severity, joins one case, which is opened
  by the detector, at the highest severity found, and PostgreSQL holds only that case;
- the detector looks again, still finds the fault, and adds no detection and no case;
- the case's newest evidence holds every detection, the exact default rule versions, the turbine
  and its frozen telemetry, every hash verifying, and every earlier version still verifies;
- PostgreSQL refuses to change or delete the evidence, even for the service's own login.

It takes about 35 minutes. It reaches the gateway on a local port of its own, so it can run
alongside checks that sign in, and removes what it made.
"""

import dataclasses
import re
import secrets
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.investigation.v1 import investigation_pb2 as pb
from windops.investigation.v1 import investigation_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2, telemetry_pb2_grpc
from windops_simulator.scenarios import SCENARIOS
from windops_simulator.simulator import Simulator

from scripts.checks.fleet import asset, cases, fault_rules
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.data_quality import device_token
from scripts.checks.fleet.fault_stream import BATCH, send
from scripts.checks.fleet.telemetry import as_tenant, kubectl
from scripts.lib.tls_client import authority_file, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import GATEWAY, NAMESPACE

APPLICATION, DATA = "windops-application", "windops-data"
ASSET_HOST = "windops-asset.windops-application.svc.cluster.local"
FAULT_HOST = "windops-fault-detection.windops-application.svc.cluster.local"
CASE_HOST = "windops-investigation.windops-application.svc.cluster.local"
TELEMETRY_HOST = "windops-telemetry.windops-application.svc.cluster.local"
TENANT, FARM = "telemetry-check", "case-gate-farm"
PREFIX = "case-gate-"
# A turbine per run: the detector keeps recent samples in memory, by turbine.
TURBINE = f"{PREFIX}{secrets.token_hex(3)}"
ADMIN, ENGINEER = "case-gate-admin", "case-gate-engineer"
# Two of the gearbox oil's thermal time constants, which its model needs before it judges.
HISTORY = timedelta(minutes=60)
# The scenario's fault, sped up: at full heat 3 minutes after it starts.
RAMP = timedelta(minutes=3)
# Measured offline with the same seed: the oil model finds it about 17 minutes in, the bearing
# model about 22, the oil at high 23. The detector looks every 5 minutes.
LIVE_AT_MOST = timedelta(minutes=40)
THE_RULES = {"gearbox-oil-residual", "gearbox-bearing-residual"}
RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DETECTOR = "service:fault-detection"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def info(message: str) -> None:
    print(f"info - {message}", flush=True)


def detections_sql(statement: str) -> str:
    return fault_rules.last_line(fault_rules.sql(statement, tenant=TENANT))


def cases_sql(statement: str) -> str:
    return cases.sql(statement, tenant=TENANT)


def scenario_simulator(start: datetime) -> tuple[Simulator, Any]:
    """The gearbox scenario's turbine and fault, on this check's turbine, after an hour's health."""
    base = SCENARIOS["gearbox-temp-001"]
    spec = dataclasses.replace(base.turbines[0], tenant_id=TENANT, wind_farm_id=FARM,
                               turbine_id=TURBINE)  # fmt: skip
    fault = dataclasses.replace(base.faults[0], turbine_id=TURBINE, onset_after=HISTORY, ramp=RAMP)
    return Simulator(base.seed, (spec,), start, (fault,)), spec


def make_turbine(assets: Caller, spec: Any) -> None:
    try:
        assets.call("CreateWindFarm", asset_pb2.CreateWindFarmRequest(
            wind_farm_id=FARM, name="Case gate", country="GB", time_zone="Europe/London",
            location=asset_pb2.Location(latitude=54.0, longitude=1.0), offshore=True,
        ), ADMIN, TENANT)  # fmt: skip
    except grpc.RpcError as error:
        if error.code() != grpc.StatusCode.ALREADY_EXISTS:
            raise
    assets.call("CreateTurbine", asset_pb2.CreateTurbineRequest(
        turbine_id=TURBINE, wind_farm_id=FARM, name="Case gate T01",
        location=asset_pb2.Location(latitude=54.0, longitude=1.0),
        model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic", model="SG-8.0-167",
                                     rated_power_kw=spec.rated_power_kw,
                                     rotor_diameter_m=spec.rotor_diameter_m),
    ), ADMIN, TENANT)  # fmt: skip


def hold_detector(hold: bool) -> None:
    """Hold the turbine back from the detector, or let it look at its next pass."""
    value = "now()" if hold else "NULL"
    detections_sql(
        "INSERT INTO turbine_watch (tenant_id, turbine_id, last_seen_at, last_evaluated_at) "  # noqa: S608  # Constants only.
        f"VALUES ('{TENANT}', '{TURBINE}', now(), {value}) ON CONFLICT (tenant_id, turbine_id) "
        f"DO UPDATE SET last_evaluated_at = {value}; SELECT 1"
    )


def as_payload(sample: Any) -> dict[str, Any]:
    payload = dict(sample.as_dict())
    payload["source"] = "case-gate"
    return payload


def progress() -> tuple[int, dict[str, str], int]:
    """Cases on the turbine, each rule's highest severity in them, and how many detections."""
    count = cases_sql(f"SELECT count(*) FROM cases WHERE turbine_id = '{TURBINE}'")  # noqa: S608
    rows = cases_sql(
        # "none", never empty: an empty result's last line would be set_config's.
        "SELECT coalesce(string_agg(d.rule_id || ':' || d.severity, ','), 'none') "  # noqa: S608
        "FROM case_detections d JOIN cases c USING (tenant_id, case_id) "
        f"WHERE c.turbine_id = '{TURBINE}'"
    )
    if rows == "none":
        return int(count), {}, 0
    found: dict[str, str] = {}
    for item in filter(None, rows.split(",")):
        rule, severity = item.split(":")
        if RANK[severity] > RANK.get(found.get(rule, ""), 0):
            found[rule] = severity
    return int(count), found, len(list(filter(None, rows.split(","))))


def seen_again() -> bool:
    """Whether a later look found an open detection again, and kept it rather than adding one."""
    return detections_sql(
        f"SELECT count(*) FROM detections WHERE turbine_id = '{TURBINE}' AND state = 'open' "  # noqa: S608
        "AND last_seen_at > detected_at"
    ) not in ("", "0")


def stream(token: str, simulator: Simulator, onset: datetime) -> None:
    """The hour before the fault at once, then the fault live, until the gate has what it needs."""
    context = ssl.create_default_context(cafile=authority_file())
    with port_forward(GATEWAY, NAMESPACE, 443) as port, httpx.Client(verify=context) as client:
        url = f"https://localhost:{port}/ingest/v1/samples"
        history = [as_payload(s) for _ in range(int(HISTORY.total_seconds()))
                   for s in simulator.step(1.0)]  # fmt: skip
        for first in range(0, len(history), 600):
            send(client, url, token, history[first : first + 600])
        info(f"sent the hour before the fault: {len(history)} samples")
        hold_detector(False)
        found: dict[str, str] = {}
        due = onset
        while datetime.now(UTC) < onset + LIVE_AT_MOST:
            due += BATCH
            time.sleep(max(0.0, (due - datetime.now(UTC)).total_seconds()))
            batch = [as_payload(s) for _ in range(int(BATCH.total_seconds()))
                     for s in simulator.step(1.0)]  # fmt: skip
            send(client, url, token, batch)
            if int((due - onset).total_seconds()) % 60:
                continue
            count, now_found, _ = progress()
            if now_found != found:
                minutes = int((due - onset).total_seconds() // 60)
                info(f"{minutes} min into the fault: {count} case(s), found {now_found}")
                found = now_found
            high = max((RANK[s] for s in found.values()), default=0) >= RANK["high"]
            if set(found) >= THE_RULES and high and seen_again():
                return
    raise RuntimeError(f"Failed: the default rules found the fault in time (found {found})")


def check_one_case(engineer: Caller, rules: Caller, onset: datetime) -> Any:
    listed = engineer.call("ListCases", pb.ListCasesRequest(
        turbine_id=TURBINE, statuses=["open", "investigating", "pending_review", "closed",
                                      "cancelled"]), ENGINEER, TENANT).cases  # fmt: skip
    check(len(listed) == 1, f"The fault made one case, and only one: {len(listed)}")
    got = engineer.call("GetCase", pb.GetCaseRequest(case_id=listed[0].case_id), ENGINEER, TENANT)
    case = got.case
    check(
        case.origin == "detection" and case.created_by == DETECTOR
        and case.component_group == "gearbox" and case.status == "open",
        f"The detector opened it, for the gearbox: {case.title}",
    )  # fmt: skip
    detections = []
    for state in ("open", "closed"):
        request = fd.ListDetectionsRequest(turbine_id=TURBINE, state=state, page_size=500)
        detections += rules.call("ListDetections", request, ENGINEER, TENANT).detections
    first = min(d.detected_at.ToDatetime(UTC) for d in detections)
    check(
        first > onset,
        f"Nothing was found in the hour before the fault; the first detection came "
        f"{(first - onset).total_seconds() / 60:.0f} minutes after it began",
    )
    check(
        {d.rule_id for d in detections} >= THE_RULES
        and all(d.rule_version == 1 for d in detections),
        "The default rules found it on their own: "
        + ", ".join(sorted(f"{d.rule_id} v{d.rule_version} {d.severity}" for d in detections)),
    )
    check(
        {d.detection_id for d in detections} == {d.detection_id for d in got.detections},
        f"All {len(detections)} detections, from both rules and each change of severity, "
        "joined that one case",
    )
    highest = max(detections, key=lambda d: RANK[d.severity]).severity
    check(case.severity == highest, f"The case is at the highest severity found: {highest}")
    open_rules = [d.rule_id for d in detections if d.state == "open"]
    again = [d for d in detections if d.state == "open"
             and d.last_seen_at.ToDatetime(UTC) > d.detected_at.ToDatetime(UTC)]  # fmt: skip
    check(
        len(open_rules) == len(set(open_rules)) and bool(again),
        f"The detector looked again and still found it, and kept {len(again)} detection(s) "
        "rather than adding any: one open detection per rule",
    )
    in_database = cases_sql(
        f"SELECT count(*) FROM cases WHERE turbine_id = '{TURBINE}'"  # noqa: S608
    )
    check(in_database == "1", "PostgreSQL holds only that case for the turbine")
    return got


def wait_for_evidence(engineer: Caller, got: Any) -> Any:
    """Until the newest evidence covers every detection in the case."""
    wanted = {d.detection_id for d in got.detections}
    for _ in range(36):
        case = engineer.call("GetCase", pb.GetCaseRequest(case_id=got.case.case_id), ENGINEER,
                             TENANT).case  # fmt: skip
        if case.evidence_status == "ready" and case.evidence_snapshot_id:
            snapshot = engineer.call("GetEvidenceSnapshot", pb.GetEvidenceSnapshotRequest(
                snapshot_id=case.evidence_snapshot_id), ENGINEER, TENANT).snapshot  # fmt: skip
            held = {i.record_id for i in snapshot.items if i.record_type == "detection"}
            if held >= wanted:
                return case, snapshot
        time.sleep(5)
    raise RuntimeError("Failed: the case's newest evidence holds every detection")


def check_evidence(engineer: Caller, telemetry: Caller, got: Any) -> None:
    case, snapshot = wait_for_evidence(engineer, got)
    kinds = {i.record_type for i in snapshot.items}
    rule_versions = {i.record_id for i in snapshot.items if i.record_type == "fault_rule_version"}
    check(
        snapshot.verified and {"turbine", "telemetry_snapshot", "detection"} <= kinds
        and rule_versions >= {f"{rule}@1" for rule in THE_RULES},
        f"Its newest evidence, version {snapshot.version}, holds the turbine, its telemetry, all "
        f"{len(got.detections)} detections and their exact rule versions: "
        f"{snapshot.item_count} items, every hash verifying",
    )  # fmt: skip
    (frozen_item,) = [i for i in snapshot.items if i.record_type == "telemetry_snapshot"]
    frozen = telemetry.call("GetTelemetrySnapshot", telemetry_pb2.GetTelemetrySnapshotRequest(
        snapshot_id=frozen_item.record_id), ENGINEER, TENANT).snapshot  # fmt: skip
    check(
        frozen.verified and frozen.content_hash == frozen_item.version,
        f"The Telemetry Service verifies the {frozen.sample_count} samples it froze for it",
    )
    versions = engineer.call("ListEvidenceSnapshots", pb.ListEvidenceSnapshotsRequest(
        case_id=case.case_id), ENGINEER, TENANT).snapshots  # fmt: skip
    earlier = [
        engineer.call(
            "GetEvidenceSnapshot",
            pb.GetEvidenceSnapshotRequest(snapshot_id=s.snapshot_id),
            ENGINEER,
            TENANT,
        ).snapshot
        for s in versions
        if s.snapshot_id != snapshot.snapshot_id
    ]
    check(
        all(e.verified for e in earlier),
        f"Each of the {len(earlier)} earlier version(s) still verifies, unchanged since",
    )
    snapshot_id = case.evidence_snapshot_id
    if not re.fullmatch(r"evs_[a-z0-9]+", snapshot_id):
        raise RuntimeError(f"Unexpected snapshot ID: {snapshot_id!r}")
    for statement in (
        f"UPDATE evidence_items SET content = '{{}}' WHERE snapshot_id = '{snapshot_id}'",  # noqa: S608  # The ID is checked above.
        f"DELETE FROM evidence_snapshots WHERE snapshot_id = '{snapshot_id}'",  # noqa: S608
    ):
        refused = kubectl(
            "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "-q", "-At",
            f"--username={cases.DATABASE}", f"--dbname={cases.DATABASE}", "--set=ON_ERROR_STOP=1",
            f"--command=SELECT set_config('windops.tenant_id', '{TENANT}', false); {statement}",
            namespace=DATA,
        )  # fmt: skip
        check(refused.returncode != 0 and "never changes" in refused.stderr,
              f"PostgreSQL refuses to {statement.split()[0].lower()} it, even for the service's "
              "own login")  # fmt: skip


def clean_up() -> None:
    """Every turbine the gate has made, and all it left: telemetry, detections, cases, evidence."""
    mine = f"turbine_id LIKE '{PREFIX}%'"
    # Constants only, never input.
    frozen = f"(SELECT snapshot_id FROM telemetry_snapshots WHERE {mine})"  # noqa: S608
    held = f"(SELECT hold_id FROM telemetry_snapshots WHERE {mine})"  # noqa: S608
    statements = [
        f"DELETE FROM snapshot_samples WHERE snapshot_id IN {frozen}",  # noqa: S608
        f"DELETE FROM retention_holds WHERE hold_id IN {held}",  # noqa: S608
    ]
    for table in ("telemetry_snapshots", "operational_samples", "scada_summaries", "data_gaps",
                  "ingest_rejections", "device_credentials"):  # fmt: skip
        statements.append(f"DELETE FROM {table} WHERE {mine}")  # noqa: S608
    as_tenant(TENANT, "; ".join([*statements, "SELECT 1"]))
    detections_sql(f"DELETE FROM detections WHERE {mine}; "  # noqa: S608
                   f"DELETE FROM turbine_watch WHERE {mine}; SELECT 1")  # fmt: skip
    cases_of = f"(SELECT case_id FROM cases WHERE {mine})"  # noqa: S608
    cases_sql(
        "ALTER TABLE evidence_items DISABLE TRIGGER evidence_items_never_change; "  # noqa: S608
        "ALTER TABLE evidence_snapshots DISABLE TRIGGER evidence_snapshots_never_change; "
        "DELETE FROM evidence_items WHERE snapshot_id IN (SELECT snapshot_id FROM "
        f"evidence_snapshots WHERE {mine}); DELETE FROM evidence_snapshots WHERE {mine}; "
        "ALTER TABLE evidence_items ENABLE TRIGGER evidence_items_never_change; "
        "ALTER TABLE evidence_snapshots ENABLE TRIGGER evidence_snapshots_never_change; "
        f"DELETE FROM case_history WHERE case_id IN {cases_of}; "
        f"DELETE FROM case_detections WHERE case_id IN {cases_of}; "
        f"DELETE FROM cases WHERE {mine}; SELECT 1"
    )
    asset.sql(
        f"SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608
        f"DELETE FROM components WHERE {mine}; DELETE FROM turbines WHERE {mine}; SELECT 1"
    )


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        start = datetime.now(UTC).replace(microsecond=0) - HISTORY
        simulator, spec = scenario_simulator(start)
        with (
            port_forward("deployment/windops-asset", APPLICATION, 50051) as port,
            blocking_channel(port, ASSET_HOST, bundle) as channel,
        ):
            make_turbine(Caller(asset_pb2_grpc.AssetServiceStub(channel), key), spec)
        info(f"{TURBINE} streams the gearbox fault from {start + HISTORY:%H:%M:%S}, after an "
             f"hour of history")  # fmt: skip
        hold_detector(True)
        stream(device_token(TENANT, TURBINE), simulator, start + HISTORY)
        with (
            port_forward("deployment/windops-fault-detection", APPLICATION, 50051) as fault_port,
            blocking_channel(fault_port, FAULT_HOST, bundle) as fault_channel,
            port_forward("deployment/windops-investigation", APPLICATION, 50051) as case_port,
            blocking_channel(case_port, CASE_HOST, bundle) as case_channel,
            port_forward("deployment/windops-telemetry", APPLICATION, 50051) as telemetry_port,
            blocking_channel(telemetry_port, TELEMETRY_HOST, bundle) as telemetry_channel,
        ):
            rules = Caller(fault_detection_pb2_grpc.FaultRuleServiceStub(fault_channel), key)
            engineer = Caller(investigation_pb2_grpc.InvestigationServiceStub(case_channel), key)
            telemetry = Caller(telemetry_pb2_grpc.TelemetryServiceStub(telemetry_channel), key)
            got = check_one_case(engineer, rules, start + HISTORY)
            check_evidence(engineer, telemetry, got)
    except (
        RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Fault case check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
