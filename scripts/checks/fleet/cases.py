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

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.detection import TURBINE, check, make_due
from scripts.checks.fleet.detection import clean_up as clean_up_rules
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
FAULT_HOST = "windops-fault-detection.windops-application.svc.cluster.local"
HOST = "windops-investigation.windops-application.svc.cluster.local"
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
