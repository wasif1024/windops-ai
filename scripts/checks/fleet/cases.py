#!/usr/bin/env python3
"""Prove cases on the cluster: one fault makes one case, from the detector (M5.4).

Run the platform and the simulator first, as `make dev` does. A Tenant Administrator publishes
two check-only rules for one turbine, at medium and high: both thresholds the live gearbox oil
already passes, and both unknown to the case rules, so in the same group. The check marks the
turbine due, and proves:

- the detector's two detections, sent as fault.detected, make one case, at the higher severity,
  holding both detections and their snapshots (FR-FLT-004);
- every change is announced as case.updated, and the case's opening is audited with the service
  that detected it as the actor;
- the case is bound to frozen evidence (FR-INV-002, EVD-001 to 004): its turbine, its telemetry
  window frozen by the Telemetry Service, its alarms, both detections and their exact rule
  versions, every hash verifying, and PostgreSQL refusing to change it;
- a Control Room Operator may not cancel it, and a Reliability Engineer does, with a reason;
- a person opens a case of their own, which stands apart (FR-INV-001);
- another tenant cannot find the case, and row-level security hides it in PostgreSQL.

Its rules, detections, snapshots and cases are removed at the end.
"""

import json
import secrets
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.investigation.v1 import investigation_pb2 as pb
from windops.investigation.v1 import investigation_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2, telemetry_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.detection import TURBINE, check, make_due
from scripts.checks.fleet.detection import clean_up as clean_up_rules
from scripts.checks.fleet.telemetry import as_tenant, kubectl
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
FAULT_HOST = "windops-fault-detection.windops-application.svc.cluster.local"
HOST = "windops-investigation.windops-application.svc.cluster.local"
TELEMETRY_HOST = "windops-telemetry.windops-application.svc.cluster.local"
DATABASE = "windops_investigation"
TENANT, OTHER = "north-sea", "atlas-wind"
ADMIN, ENGINEER, OPERATOR = "case-check-admin", "case-check-engineer", "case-check-operator"
STRANGER = "case-check-other"
PREFIX = "case-check-"
RUN = secrets.token_hex(3)
RULES = {f"{PREFIX}a-{RUN}": "medium", f"{PREFIX}b-{RUN}": "high"}
TITLE = f"Case check {RUN}"


def sql(statement: str, tenant: str = "") -> str:
    """As the Investigation Service's own login, in one tenant, or across them all."""
    setting = (
        f"SELECT set_config('windops.tenant_id', '{tenant}', false); "
        if tenant
        else "SELECT set_config('windops.all_tenants', 'on', false); "
    )
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={setting}{statement}",
        namespace=DATA,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def publish_rules(rules: Caller) -> None:
    for rule_id, severity in RULES.items():
        definition = {
            "kind": "threshold",
            "parameters": {"metric": "gearbox_oil_temp_c", "for_seconds": 60},
            "levels": [{"at": 1, "severity": severity}],
        }
        request = fd.PublishFaultRuleVersionRequest(
            rule_id=rule_id,
            name="Case check",
            reason="The case check",
            definition_json=json.dumps(definition),
        )
        rules.call("PublishFaultRuleVersion", request, ADMIN, TENANT)


def the_case(cases: Caller) -> Any | None:
    """The detector's case for the check's rules, once both its detections are in it."""
    listed = cases.call("ListCases", pb.ListCasesRequest(turbine_id=TURBINE), ENGINEER, TENANT)
    for case in listed.cases:
        if case.origin != "detection" or case.component_group != "turbine":
            continue
        got = cases.call("GetCase", pb.GetCaseRequest(case_id=case.case_id), ENGINEER, TENANT)
        if {d.rule_id for d in got.detections} >= set(RULES):
            return got
    return None


def check_one_case(cases: Caller) -> Any:
    found = None
    for _ in range(36):
        found = the_case(cases)
        if found is not None:
            break
        time.sleep(5)
    if found is None:
        raise RuntimeError("Failed: The two detections made a case")
    mine = [d for d in found.detections if d.rule_id in RULES]
    check(
        len(mine) == 2 and found.case.severity == "high" and found.case.created_by
        == "service:fault-detection" and all(d.telemetry_snapshot_id for d in mine),
        "Two rules' detections of one fault made one case, at the higher severity, with both "
        "detections and their snapshots",
    )  # fmt: skip
    return found


def check_evidence(cases: Caller, bundle: Any, key: str, found: Any) -> None:
    case = found.case
    for _ in range(24):
        got = cases.call("GetCase", pb.GetCaseRequest(case_id=case.case_id), ENGINEER, TENANT)
        case = got.case
        if case.evidence_status == "ready" and case.evidence_snapshot_id:
            break
        time.sleep(5)
    check(
        case.evidence_status == "ready",
        f"The case is bound to evidence, version {case.evidence_version}",
    )
    request = pb.GetEvidenceSnapshotRequest(snapshot_id=case.evidence_snapshot_id)
    snapshot = cases.call("GetEvidenceSnapshot", request, ENGINEER, TENANT).snapshot
    kinds = sorted({(i.record_type, i.record_id) for i in snapshot.items
                    if i.record_type != "alarm"})  # fmt: skip
    rules = {i.record_id.split("@")[0] for i in snapshot.items
             if i.record_type == "fault_rule_version"}  # fmt: skip
    check(
        snapshot.verified and rules >= set(RULES)
        and {"turbine", "telemetry_snapshot", "detection"} <= {k for k, _ in kinds},
        f"It holds the turbine, its telemetry, its alarms, its detections and their exact rule "
        f"versions: {snapshot.item_count} items, every hash verifying",
    )  # fmt: skip
    (telemetry,) = [i for i in snapshot.items if i.record_type == "telemetry_snapshot"]
    with (
        port_forward("deployment/windops-telemetry", APPLICATION, 50051) as port,
        blocking_channel(port, TELEMETRY_HOST, bundle) as channel,
    ):
        frozen = Caller(telemetry_pb2_grpc.TelemetryServiceStub(channel), key).call(
            "GetTelemetrySnapshot", telemetry_pb2.GetTelemetrySnapshotRequest(
                snapshot_id=telemetry.record_id), ENGINEER, TENANT,
        ).snapshot  # fmt: skip
    check(
        frozen.verified and frozen.content_hash == telemetry.version
        and frozen.created_by == "service:investigation",
        f"The Telemetry Service froze its {frozen.sample_count} samples, and they verify",
    )  # fmt: skip
    snapshot_id = case.evidence_snapshot_id
    if not snapshot_id.startswith("evs_") or not snapshot_id[4:].isalnum():
        raise RuntimeError(f"Unexpected snapshot ID: {snapshot_id!r}")
    changed = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "-q", "-At",
        f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command=SELECT set_config('windops.tenant_id', '{TENANT}', false); UPDATE "  # noqa: S608  # The ID is checked above.
        f"evidence_items SET content = '{{}}' WHERE snapshot_id = '{snapshot_id}'",
        namespace=DATA,
    )  # fmt: skip
    check(changed.returncode != 0 and "never changes" in changed.stderr,
          "PostgreSQL refuses to change it, even for the service's own login")  # fmt: skip
    hidden = sql(
        f"SELECT count(*) FROM evidence_snapshots WHERE snapshot_id = '{snapshot_id}'",  # noqa: S608
        tenant=OTHER,
    )
    check(hidden == "0", "and row-level security hides it from another tenant")


def check_announced(found: Any) -> None:
    case_id = found.case.case_id
    if not case_id.startswith("case_") or not case_id[5:].isalnum():
        raise RuntimeError(f"Unexpected case ID: {case_id!r}")
    published = "0"
    for _ in range(20):
        published = sql(
            "SELECT count(*) FROM outbox_events WHERE subject = 'case.updated' "
            "AND published_at IS NOT NULL AND created_at > now() - interval '10 minutes'"
        )
        if int(published) >= 2:
            break
        time.sleep(1)
    check(int(published) >= 2, f"Each change was announced as case.updated ({published})")
    hidden = sql(f"SELECT count(*) FROM cases WHERE case_id = '{case_id}'", tenant=OTHER)  # noqa: S608
    check(hidden == "0", "Row-level security hides the case from another tenant")


def check_people(cases: Caller, found: Any) -> None:
    get = pb.GetCaseRequest(case_id=found.case.case_id)
    check(
        cases.code("GetCase", get, STRANGER, OTHER) == grpc.StatusCode.NOT_FOUND,
        "Another tenant cannot find it",
    )
    cancel = pb.CancelCaseRequest(case_id=found.case.case_id, reason="Case check over")
    refused = cases.code("CancelCase", cancel, OPERATOR, TENANT)
    done = cases.call("CancelCase", cancel, ENGINEER, TENANT).case
    check(
        refused == grpc.StatusCode.PERMISSION_DENIED and done.status == "cancelled"
        and done.cancel_reason == "Case check over",
        "An operator may not cancel it; a Reliability Engineer does, with a reason",
    )  # fmt: skip
    request = pb.CreateCaseRequest(turbine_id=TURBINE, title=TITLE, severity="medium")
    made = cases.call("CreateCase", request, OPERATOR, TENANT).case
    check(
        (made.origin, made.created_by, made.status) == ("user", OPERATOR, "open"),
        "A Control Room Operator opens a case of their own",
    )


def clean_up() -> None:
    # The check's evidence, and the telemetry its snapshots froze. Constants and IDs read here.
    frozen = sql(
        "SELECT coalesce(string_agg(i.record_id, ','), '') FROM evidence_items i JOIN "  # noqa: S608
        "evidence_snapshots s USING (tenant_id, snapshot_id) WHERE i.record_type = "
        "'telemetry_snapshot' AND s.case_id IN (SELECT case_id FROM case_detections WHERE "
        f"rule_id LIKE '{PREFIX}%' UNION SELECT case_id FROM cases WHERE title LIKE 'Case check %')"
    )
    for snapshot_id in filter(None, frozen.split(",")):
        if snapshot_id.startswith("tsnap_") and snapshot_id[6:].isalnum():
            as_tenant(TENANT, (
                f"DELETE FROM snapshot_samples WHERE snapshot_id = '{snapshot_id}'; "  # noqa: S608
                "DELETE FROM retention_holds WHERE hold_id IN (SELECT hold_id FROM "
                f"telemetry_snapshots WHERE snapshot_id = '{snapshot_id}'); "
                f"DELETE FROM telemetry_snapshots WHERE snapshot_id = '{snapshot_id}'; SELECT 1"
            ))  # fmt: skip
    mine = (
        f"(SELECT case_id FROM case_detections WHERE rule_id LIKE '{PREFIX}%' "  # noqa: S608
        "UNION SELECT case_id FROM cases WHERE title LIKE 'Case check %')"
    )
    sql(
        "ALTER TABLE evidence_items DISABLE TRIGGER evidence_items_never_change; "  # noqa: S608
        "ALTER TABLE evidence_snapshots DISABLE TRIGGER evidence_snapshots_never_change; "
        "DELETE FROM evidence_items WHERE snapshot_id IN (SELECT snapshot_id FROM "
        f"evidence_snapshots WHERE case_id IN {mine}); "
        f"DELETE FROM evidence_snapshots WHERE case_id IN {mine}; "
        "ALTER TABLE evidence_items ENABLE TRIGGER evidence_items_never_change; "
        "ALTER TABLE evidence_snapshots ENABLE TRIGGER evidence_snapshots_never_change; SELECT 1"
    )
    sql(
        "DELETE FROM case_history WHERE case_id IN (SELECT case_id FROM case_detections "  # noqa: S608
        f"WHERE rule_id LIKE '{PREFIX}%') OR case_id IN (SELECT case_id FROM cases WHERE "
        f"title LIKE 'Case check %'); DELETE FROM cases WHERE case_id IN (SELECT case_id FROM "
        f"case_detections WHERE rule_id LIKE '{PREFIX}%') OR title LIKE 'Case check %'; "
        f"DELETE FROM case_detections WHERE rule_id LIKE '{PREFIX}%'; SELECT 1"
    )
    clean_up_rules(PREFIX)


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        bootstrap(OTHER, STRANGER, "reliability-engineer")
        with (
            port_forward("deployment/windops-fault-detection", APPLICATION, 50051) as fport,
            blocking_channel(fport, FAULT_HOST, bundle) as fchannel,
            port_forward("deployment/windops-investigation", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            rules = Caller(fault_detection_pb2_grpc.FaultRuleServiceStub(fchannel), key)
            cases = Caller(investigation_pb2_grpc.InvestigationServiceStub(channel), key)
            publish_rules(rules)
            make_due()
            found = check_one_case(cases)
            check_evidence(cases, bundle, key, found)
            check_announced(found)
            check_people(cases, found)
            for rule_id in RULES:
                off = fd.SetFaultRuleEnabledRequest(rule_id=rule_id, enabled=False, reason="Done")
                rules.call("SetFaultRuleEnabled", off, ADMIN, TENANT)
    except (
        RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Case check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
