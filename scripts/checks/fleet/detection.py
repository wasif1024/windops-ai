#!/usr/bin/env python3
"""Prove detection records on the cluster, from the detector itself (M5.3, FR-FLT-003).

Run the platform and the simulator first, as `make dev` does. A Tenant Administrator publishes a
check-only rule on the live fleet: a low-severity threshold the live gearbox oil already passes.
The check marks one turbine due, and proves the detector, acting as the Fault Detection Service
itself (ADR 0008):

- opens one detection, holding the rule's exact version and content hash;
- has the Telemetry Service freeze the window it saw, and the snapshot verifies;
- publishes fault.detected, and the audit trail names the service as the actor;
- closes the detection, as cleared, once the rule is turned off.

Its rule, detections and snapshots are removed at the end.
"""

import json
import secrets
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.fault_detection.v1 import fault_detection_pb2 as pb
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2, telemetry_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.fault_rules import last_line, sql
from scripts.checks.fleet.telemetry import as_tenant
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION = "windops-application"
HOST = "windops-fault-detection.windops-application.svc.cluster.local"
TELEMETRY_HOST = "windops-telemetry.windops-application.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
TENANT, TURBINE = "north-sea", "dogger-east-t01"
ADMIN, ENGINEER, AUDITOR = (
    "detection-check-admin",
    "detection-check-engineer",
    "detection-check-auditor",
)
RULE = f"detection-check-{secrets.token_hex(3)}"
SERVICE = "service:fault-detection"
# Healthy gearbox oil runs well above 1 °C, so this finds it at once, at low severity.
DEFINITION = {
    "kind": "threshold",
    "parameters": {"metric": "gearbox_oil_temp_c", "for_seconds": 60},
    "levels": [{"at": 1, "severity": "low"}],
}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def make_due() -> None:
    """Evaluate the turbine at the detector's next look, not at the end of its interval."""
    last_line(sql(
        "UPDATE turbine_watch SET last_evaluated_at = NULL "  # noqa: S608
        f"WHERE turbine_id = '{TURBINE}'; SELECT 1", tenant=TENANT,
    ))  # fmt: skip


def mine(rules: Caller, state: str) -> list[Any]:
    request = pb.ListDetectionsRequest(turbine_id=TURBINE, state=state, page_size=500)
    found = rules.call("ListDetections", request, ENGINEER, TENANT).detections
    return [d for d in found if d.rule_id == RULE]


def wait_for(rules: Caller, state: str) -> list[Any]:
    for _ in range(36):
        found = mine(rules, state)
        if found:
            return found
        time.sleep(5)
    return []


def check_watched() -> None:
    for _ in range(12):
        watched = last_line(sql(
            f"SELECT count(*) FROM turbine_watch WHERE turbine_id = '{TURBINE}' "  # noqa: S608
            "AND last_seen_at > now() - interval '2 minutes'", tenant=TENANT,
        ))  # fmt: skip
        if watched == "1":
            break
        time.sleep(5)
    check(watched == "1", f"telemetry.received tells the detector {TURBINE} is streaming")


def check_detected(rules: Caller) -> Any:
    request = pb.PublishFaultRuleVersionRequest(
        rule_id=RULE,
        name="Detection check",
        reason="The detection check",
        definition_json=json.dumps(DEFINITION),
    )
    rule = rules.call("PublishFaultRuleVersion", request, ADMIN, TENANT).rule
    make_due()
    found = wait_for(rules, "open")
    check(len(found) == 1, "The detector opened one detection for the rule, as itself")
    detection = found[0]
    check(
        (detection.rule_version, detection.rule_content_hash, detection.severity)
        == (1, rule.current.content_hash, "low"),
        "It holds the rule's exact version and content hash, and the rule's own severity",
    )
    observed = json.loads(detection.observed_json)
    check(
        observed.get("metric") == "gearbox_oil_temp_c" and observed.get("least", 0) > 1,
        f"and every value it rests on: gearbox oil held at {observed.get('least')} °C",
    )
    return detection


def check_snapshot(bundle: ClientBundle, key: str, detection: Any) -> None:
    with (
        port_forward("deployment/windops-telemetry", APPLICATION, 50051) as port,
        blocking_channel(port, TELEMETRY_HOST, bundle) as channel,
    ):
        telemetry = Caller(telemetry_pb2_grpc.TelemetryServiceStub(channel), key)
        request = telemetry_pb2.GetTelemetrySnapshotRequest(
            snapshot_id=detection.telemetry_snapshot_id
        )
        snapshot = telemetry.call("GetTelemetrySnapshot", request, ENGINEER, TENANT).snapshot
    window = (detection.window_end.seconds - detection.window_start.seconds) / 3600
    check(
        snapshot.verified and snapshot.content_hash == detection.telemetry_snapshot_hash
        and snapshot.created_by == SERVICE and snapshot.sample_count > 0,
        f"The Telemetry Service froze the {window:.1f} hours it saw, {snapshot.sample_count} "
        "samples, and the snapshot verifies",
    )  # fmt: skip


def check_published(detection: Any) -> None:
    published = ""
    for _ in range(20):
        published = last_line(sql(
            "SELECT count(*) FROM outbox_events WHERE subject = 'fault.detected' "
            "AND published_at IS NOT NULL AND created_at > now() - interval '10 minutes'"
        ))  # fmt: skip
        if int(published or 0) >= 1:
            break
        time.sleep(1)
    check(int(published or 0) >= 1, "fault.detected was published, from the outbox")


def check_audit(bundle: ClientBundle, key: str, detection: Any) -> None:
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, bundle) as channel,
    ):
        audit = Caller(audit_pb2_grpc.AuditServiceStub(channel), key)
        found = False
        for _ in range(30):
            token = ""
            while not found:
                request = audit_pb2.ListAuditRecordsRequest(
                    page_size=500, page_token=token, action="fault.detected"
                )
                page = audit.call("ListAuditRecords", request, AUDITOR, TENANT)
                found = any(r.resource_id == detection.detection_id and r.actor_id == SERVICE
                            for r in page.records)  # fmt: skip
                token = page.next_page_token
                if not token:
                    break
            if found:
                break
            time.sleep(2)
    check(found, "The audit trail records the detection, with the service as its actor")


def check_closed(rules: Caller) -> None:
    off = pb.SetFaultRuleEnabledRequest(rule_id=RULE, enabled=False, reason="Check over")
    rules.call("SetFaultRuleEnabled", off, ADMIN, TENANT)
    make_due()
    closed = wait_for(rules, "closed")
    check(
        len(closed) == 1 and closed[0].closed_reason == "cleared" and not mine(rules, "open"),
        "Turned off, the rule finds nothing, and the detection closes as cleared",
    )


def clean_up() -> None:
    snapshots = last_line(sql(
        "SELECT coalesce(string_agg(telemetry_snapshot_id, ','), '') FROM detections "
        "WHERE rule_id LIKE 'detection-check-%'"
    ))  # fmt: skip
    for snapshot_id in filter(None, snapshots.split(",")):
        if not snapshot_id.startswith("tsnap_") or not snapshot_id[6:].isalnum():
            continue
        as_tenant(TENANT, (
            "DELETE FROM snapshot_samples WHERE snapshot_id = "  # noqa: S608
            f"'{snapshot_id}'; DELETE FROM retention_holds WHERE hold_id IN (SELECT hold_id FROM "
            f"telemetry_snapshots WHERE snapshot_id = '{snapshot_id}'); DELETE FROM "
            f"telemetry_snapshots WHERE snapshot_id = '{snapshot_id}'; SELECT 1"
        ))  # fmt: skip
    last_line(sql(
        "ALTER TABLE fault_rule_versions DISABLE TRIGGER fault_rule_versions_never_change; "
        "DELETE FROM fault_rule_versions WHERE rule_id LIKE 'detection-check-%'; "
        "DELETE FROM fault_rules WHERE rule_id LIKE 'detection-check-%'; "
        "DELETE FROM detections WHERE rule_id LIKE 'detection-check-%'; "
        "ALTER TABLE fault_rule_versions ENABLE TRIGGER fault_rule_versions_never_change; SELECT 1"
    ))  # fmt: skip


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, AUDITOR, "auditor")
        check_watched()
        with (
            port_forward("deployment/windops-fault-detection", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            rules = Caller(fault_detection_pb2_grpc.FaultRuleServiceStub(channel), key)
            detection = check_detected(rules)
            check_snapshot(bundle, key, detection)
            check_published(detection)
            check_audit(bundle, key, detection)
            check_closed(rules)
    except (
        RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Detection check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
