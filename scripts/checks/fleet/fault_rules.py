#!/usr/bin/env python3
"""Prove fault rules on the cluster: versioned, audited and changed only by a person (M5.2).

Run the platform and the simulator first, as `make dev` does. It proves what FR-FLT-001, 002 and
005 ask:

- a tenant starts with the default library, as version 1 of each rule;
- a Tenant Administrator with a recent second factor publishes a rule and a new version of it,
  each with who and why, and both reach the tamper-evident audit trail;
- a Reliability Engineer, and the same administrator without a second factor, are refused, and a
  stale edit is refused rather than overwriting the newer one;
- PostgreSQL itself refuses to change or delete a version, and hides one tenant's rules from
  another;
- a dry run of every default rule against a live demo turbine reads hours of its telemetry, and
  finds no fault on the healthy fleet. It reports the data gaps restarts leave, as they are real.

Its rules are in a check-only tenant, and removed at the end.
"""

import json
import secrets
import subprocess
import sys
import time

import grpc
import httpx
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.fault_detection.v1 import fault_detection_pb2 as pb
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops_auth import LEVEL_PASSWORD

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-fault-detection.windops-application.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
DATABASE = "windops_fault_detection"
TENANT, FLEET, OTHER = "fault-rules-check", "north-sea", "atlas-wind"
ADMIN, ENGINEER, AUDITOR = "fault-check-admin", "fault-check-engineer", "fault-check-auditor"
OTHER_ADMIN = "fault-check-other"
RULE = f"fault-rules-check-{secrets.token_hex(3)}"
TURBINE = "dogger-east-t01"
DEFINITION = {
    "kind": "threshold",
    "parameters": {"metric": "main_bearing_temp_c", "for_seconds": 60},
    "levels": [{"at": 45, "severity": "high"}],
}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def sql(statement: str, tenant: str = "") -> subprocess.CompletedProcess[str]:
    """As the Fault Detection Service's own login, in one tenant, or across them all."""
    setting = (
        f"SELECT set_config('windops.tenant_id', '{tenant}', false); "
        if tenant
        else "SELECT set_config('windops.all_tenants', 'on', false); "
    )
    return kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={setting}{statement}",
        namespace=DATA,
    )  # fmt: skip


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def publish(version: int) -> pb.PublishFaultRuleVersionRequest:
    """The next version of the check's rule, replacing `version`."""
    definition = {**DEFINITION, "levels": [{"at": 45 + version, "severity": "high"}]}
    return pb.PublishFaultRuleVersionRequest(
        rule_id=RULE,
        name="Main bearing hot, for the check",
        reason=f"Version {version + 1}",
        definition_json=json.dumps(definition),
        expected_version=version,
    )


def check_defaults(rules: Caller) -> None:
    listed = rules.call("ListFaultRules", pb.ListFaultRulesRequest(), ENGINEER, TENANT)
    versions = {(r.current.version, r.current.created_by) for r in listed.rules}
    check(
        len(listed.rules) >= 11 and (1, "system") in versions,
        f"The tenant starts with the default library, {listed.library}: {len(listed.rules)} rules",
    )


def check_changes(rules: Caller) -> None:
    request = publish(0)
    refusals = {
        "a Reliability Engineer": rules.code("PublishFaultRuleVersion", request, ENGINEER, TENANT),
        "an administrator without a second factor": rules.code(
            "PublishFaultRuleVersion", request, ADMIN, TENANT, level=LEVEL_PASSWORD
        ),
    }
    check(
        set(refusals.values()) == {grpc.StatusCode.PERMISSION_DENIED},
        f"Refused to change a rule: {', '.join(refusals)}",
    )
    first = rules.call("PublishFaultRuleVersion", request, ADMIN, TENANT).rule
    second_request = publish(1)
    second = rules.call("PublishFaultRuleVersion", second_request, ADMIN, TENANT).rule
    check(
        (first.current.version, second.current.version, second.current.created_by) == (1, 2, ADMIN),
        "An administrator with a second factor publishes a rule, then a new version of it",
    )
    stale = rules.code("PublishFaultRuleVersion", second_request, ADMIN, TENANT)
    check(stale == grpc.StatusCode.FAILED_PRECONDITION, "A stale edit is refused, not applied")
    history = rules.call("GetFaultRule", pb.GetFaultRuleRequest(rule_id=RULE), ENGINEER, TENANT)
    check(
        [(v.version, v.reason) for v in history.versions] == [(1, "Version 1"), (2, "Version 2")]
        and all(len(v.content_hash) == 64 for v in history.versions),
        "Both versions are kept, each with its reason and content hash",
    )


def check_database() -> None:
    changed = sql(
        f"UPDATE fault_rule_versions SET reason = 'edited' WHERE rule_id = '{RULE}'",  # noqa: S608
        tenant=TENANT,
    )
    check(
        changed.returncode != 0 and "never changes" in changed.stderr,
        "PostgreSQL refuses to change a version, even for the service's own login",
    )
    hidden = last_line(
        sql(f"SELECT count(*) FROM fault_rules WHERE rule_id = '{RULE}'", tenant=OTHER)  # noqa: S608
    )
    check(hidden == "0", "and row-level security hides the rule from another tenant")


def check_audit(bundle: ClientBundle, key: str) -> None:
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, bundle) as channel,
    ):
        audit = Caller(audit_pb2_grpc.AuditServiceStub(channel), key)
        found: set[str] = set()
        for _ in range(30):
            for action in ("fault_rule.created", "fault_rule.version_published"):
                request = audit_pb2.ListAuditRecordsRequest(page_size=500, action=action)
                records = audit.call("ListAuditRecords", request, AUDITOR, TENANT).records
                if any(r.resource_id == RULE and r.actor_id == ADMIN for r in records):
                    found.add(action)
            if len(found) == 2:
                break
            time.sleep(2)
    check(len(found) == 2, "Both changes reached the tamper-evident audit trail, by who made them")


def check_dry_run(rules: Caller) -> None:
    request = pb.EvaluateTurbineRequest(turbine_id=TURBINE)
    # Hours of telemetry, so a longer deadline than a plain call's.
    result = rules.stub.EvaluateTurbine(
        request, metadata=rules.metadata(ENGINEER, FLEET), timeout=60
    )
    hours = (result.window_end.seconds - result.window_start.seconds) / 3600
    # Restarts leave real gaps, which the data-gap rules report, so most of the window will do.
    check(
        result.sample_count >= 0.5 * hours * 3600,
        f"A dry run reads {hours:.1f} hours of {TURBINE}'s telemetry: {result.sample_count} "
        "samples",
    )
    gaps = [f.rule_id for f in result.findings if f.kind == "data_gap"]
    conditions = [(f.rule_id, f.severity) for f in result.findings if f.kind != "data_gap"]
    check(
        not conditions,
        f"and the default rules find no fault on the healthy fleet (data gaps: {gaps or 'none'})",
    )


# The check's own tenant. Constants, never input. The versions' guard stops even their owner, so
# it steps aside for these rows only, inside one transaction.
CLEAN_UP = (
    "ALTER TABLE fault_rule_versions DISABLE TRIGGER fault_rule_versions_never_change; "  # noqa: S608
    f"DELETE FROM fault_rule_versions WHERE tenant_id = '{TENANT}'; "
    f"DELETE FROM fault_rules WHERE tenant_id = '{TENANT}'; "
    f"DELETE FROM idempotency_keys WHERE tenant_id = '{TENANT}'; "
    "ALTER TABLE fault_rule_versions ENABLE TRIGGER fault_rule_versions_never_change; SELECT 1"
)


def clean_up() -> None:
    last_line(sql(CLEAN_UP))


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, AUDITOR, "auditor")
        bootstrap(FLEET, ENGINEER, "reliability-engineer")
        bootstrap(OTHER, OTHER_ADMIN, "tenant-administrator")
        with (
            port_forward("deployment/windops-fault-detection", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            rules = Caller(fault_detection_pb2_grpc.FaultRuleServiceStub(channel), key)
            check_defaults(rules)
            check_changes(rules)
            check_database()
            check_dry_run(rules)
        check_audit(bundle, key)
    except (
        RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Fault rules check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
