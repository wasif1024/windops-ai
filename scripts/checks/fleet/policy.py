#!/usr/bin/env python3
"""Prove the Policy Service on the cluster: proposals judged, results recorded for ever (M6.1).

Run the platform first, as `make dev` does. A Control Room Operator opens a check-only case on a
demo turbine, and once its evidence is frozen, the check proves:

- a tenant starts with the default library, policy-rules-1, readable with policy.read;
- a sound proposal passes, with every rule version and input recorded: the case and its evidence
  from the Investigation Service and the turbine and its site from the Asset Service, gathered
  by the Policy Service as itself (ADR 0009);
- work at height blocks while the weather cannot be known, which it cannot until M6.3, and an
  unstated confidence or a cost over the limit escalates (FR-POL-005);
- a Tenant Administrator, with a recent second factor, gives a site a limit of its own as a new
  rule version, and the next proposal there is judged by it; a password alone is refused;
- a result reads back verified, another tenant cannot find it, and the audit trail records it;
- PostgreSQL refuses to change or delete a result or a rule version, even for the service's own
  login, and row-level security hides them from another tenant.

Its case, rule and results are removed at the end.
"""

import json
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.investigation.v1 import investigation_pb2, investigation_pb2_grpc
from windops.policy.v1 import policy_pb2 as pb
from windops.policy.v1 import policy_pb2_grpc
from windops_auth import LEVEL_PASSWORD

from scripts.checks.fleet import cases
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-policy.windops-application.svc.cluster.local"
CASE_HOST = "windops-investigation.windops-application.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
DATABASE = "windops_policy"
TENANT, OTHER, TURBINE, SITE = "north-sea", "atlas-wind", "dogger-east-t01", "dogger-east"
ADMIN, ENGINEER, OPERATOR = "policy-check-admin", "policy-check-engineer", "policy-check-operator"
AUDITOR, STRANGER = "policy-check-auditor", "policy-check-other"
RUN = secrets.token_hex(3)
RULE = f"policy-check-{RUN}"
# Titled so the case check's clean-up removes it, with its evidence.
TITLE = f"Case check policy {RUN}"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def psql(statement: str, tenant: str = "") -> subprocess.CompletedProcess[str]:
    """As the Policy Service's own login, in one tenant, or across them all."""
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


def sql(statement: str, tenant: str = "") -> str:
    result = psql(statement, tenant)
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def stamp(at: datetime) -> Timestamp:
    value = Timestamp()
    value.FromDatetime(at)
    return value


def sound(**changes: Any) -> pb.Proposal:
    fields: dict[str, Any] = {
        "action": "inspection", "summary": "Inspect the gearbox oil cooler",
        "requires_climb": False, "estimated_cost_eur": 2000.0, "estimated_downtime_hours": 4.0,
        "confidence": 0.8,
    }  # fmt: skip
    fields.update(changes)
    return pb.Proposal(**{k: v for k, v in fields.items() if v is not None})


def open_case(investigation: Caller) -> str:
    request = investigation_pb2.CreateCaseRequest(turbine_id=TURBINE, title=TITLE,
                                                  severity="high")  # fmt: skip
    case_id = str(investigation.call("CreateCase", request, OPERATOR, TENANT).case.case_id)
    for _ in range(36):
        case = investigation.call(
            "GetCase", investigation_pb2.GetCaseRequest(case_id=case_id), ENGINEER, TENANT
        ).case
        if case.evidence_status == "ready" and case.evidence_snapshot_id:
            break
        time.sleep(5)
    check(case.evidence_status == "ready", f"An operator's case on {TURBINE} has frozen evidence")
    return case_id


def evaluate(policy: Caller, case_id: str, proposal: pb.Proposal, key: str = "") -> Any:
    request = pb.EvaluateProposalRequest(case_id=case_id, proposal=proposal, idempotency_key=key)
    return policy.call("EvaluateProposal", request, ENGINEER, TENANT).result


def outcome_of(result: Any, rule_id: str) -> Any:
    return next(r for r in result.rules if r.rule_id == rule_id)


def check_judgements(policy: Caller, case_id: str) -> Any:
    listed = policy.call("ListPolicyRules", pb.ListPolicyRulesRequest(), ADMIN, TENANT)
    check(
        listed.library == "policy-rules-1" and len(listed.rules) >= 10
        and all(r.current.created_by == "system" for r in listed.rules if r.rule_id != RULE),
        f"The tenant has the default library, {listed.library}: {len(listed.rules)} rules",
    )  # fmt: skip
    passed = evaluate(policy, case_id, sound(), key=f"policy-check-{RUN}-1")
    inputs = {i.name: i for i in passed.inputs}
    check(
        passed.outcome == "pass" and passed.verified and passed.wind_farm_id == SITE
        and inputs["case.severity"].source == "investigation"
        and inputs["case.severity"].source_ref.startswith(f"{case_id}@")
        and inputs["turbine.status"].source == "asset"
        and inputs["evidence.age_hours"].state == "available",
        f"A sound proposal passes, judged by {len(passed.rules)} rule versions on inputs gathered "
        "from Investigation and Asset",
    )  # fmt: skip
    again = evaluate(policy, case_id, sound(), key=f"policy-check-{RUN}-1")
    check(again.result_id == passed.result_id, "A retry returns the first result, not a second")
    climb = evaluate(policy, case_id, sound(
        requires_climb=True, planned_start=stamp(datetime.now(UTC) + timedelta(hours=2)),
        planned_end=stamp(datetime.now(UTC) + timedelta(hours=6)),
    ))  # fmt: skip
    wind = outcome_of(climb, "work-at-height-wind")
    check(
        climb.outcome == "block" and wind.outcome == "block" and "unavailable" in wind.reason,
        f"Work at height blocks while the weather cannot be known: {wind.reason}",
    )
    costly = evaluate(policy, case_id, sound(estimated_cost_eur=60000.0, confidence=None))
    check(
        costly.outcome == "escalate"
        and outcome_of(costly, "cost-limit").outcome == "escalate"
        and outcome_of(costly, "low-confidence").outcome == "escalate",
        "A cost over the limit and an unstated confidence escalate",
    )
    return passed


def check_site_limit(policy: Caller, case_id: str) -> None:
    definition = {
        "kind": "limit", "category": "business",
        "parameters": {"input": "proposal.estimated_downtime_hours", "above": 100.0,
                       "sites": {SITE: 2.0}},
        "outcome": "escalate", "when_missing": "escalate",
    }  # fmt: skip
    request = pb.PublishPolicyRuleVersionRequest(
        rule_id=RULE, name="Policy check downtime", definition_json=json.dumps(definition),
        reason="Policy check: a site's own limit", expected_version=0,
        idempotency_key=f"policy-check-{RUN}-rule",
    )  # fmt: skip
    refused = policy.code("PublishPolicyRuleVersion", request, ADMIN, TENANT, level=LEVEL_PASSWORD)
    published = policy.call("PublishPolicyRuleVersion", request, ADMIN, TENANT).rule
    check(
        refused == grpc.StatusCode.PERMISSION_DENIED and published.current.version == 1
        and len(published.current.content_hash) == 64,
        "A Tenant Administrator publishes a rule only with a recent second factor",
    )  # fmt: skip
    judged = outcome_of(evaluate(policy, case_id, sound()), RULE)
    check(
        (judged.outcome, judged.limit_from, judged.limit_json) == ("escalate", SITE, "2.0"),
        f"{SITE} has a limit of its own, and the next proposal there is judged by it",
    )


def check_reads(policy: Caller, result: Any) -> None:
    get = pb.GetPolicyResultRequest(result_id=result.result_id)
    read = policy.call("GetPolicyResult", get, ENGINEER, TENANT).result
    check(read.verified and read.content_hash == result.content_hash,
          "The result reads back unchanged, and its hash verifies")  # fmt: skip
    check(policy.code("GetPolicyResult", get, STRANGER, OTHER) == grpc.StatusCode.NOT_FOUND,
          "Another tenant cannot find it")  # fmt: skip


def check_audit(bundle: ClientBundle, key: str, result: Any) -> None:
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, bundle) as channel,
    ):
        audit = Caller(audit_pb2_grpc.AuditServiceStub(channel), key)
        found = False
        for _ in range(30):
            request = audit_pb2.ListAuditRecordsRequest(page_size=500, action="policy.evaluated")
            page = audit.call("ListAuditRecords", request, AUDITOR, TENANT)
            found = any(r.resource_id == result.result_id and r.actor_id == ENGINEER
                        for r in page.records)  # fmt: skip
            if found:
                break
            time.sleep(2)
    check(found, "The audit trail records the evaluation, with the engineer as its actor")


def check_database(result: Any) -> None:
    if not re.fullmatch(r"pol_[a-z0-9]{20}", result.result_id):
        raise RuntimeError(f"Unexpected result ID: {result.result_id!r}")
    for statement in (
        f"UPDATE policy_results SET outcome = 'pass' WHERE result_id = '{result.result_id}'",  # noqa: S608  # The ID is checked above.
        f"DELETE FROM policy_results WHERE result_id = '{result.result_id}'",  # noqa: S608
        "UPDATE policy_rule_versions SET reason = 'changed' WHERE rule_id = 'cost-limit'",
    ):
        refused = psql(statement, TENANT)
        check(refused.returncode != 0 and "never changes" in refused.stderr,
              f"PostgreSQL refuses: {statement.split(' WHERE')[0]}")  # fmt: skip
    hidden = sql(f"SELECT count(*) FROM policy_results WHERE result_id = '{result.result_id}'",  # noqa: S608
                 OTHER)  # fmt: skip
    check(hidden == "0", "Row-level security hides the result from another tenant")


def clean_up() -> None:
    """The check's results and rule. Its case goes with the case check's clean-up."""
    sql(
        "ALTER TABLE policy_results DISABLE TRIGGER policy_results_never_change; "
        "ALTER TABLE policy_rule_versions DISABLE TRIGGER policy_rule_versions_never_change; "
        "DELETE FROM policy_results WHERE evaluated_by LIKE 'policy-check-%'; "
        "DELETE FROM policy_rule_versions WHERE rule_id LIKE 'policy-check-%'; "
        "DELETE FROM policy_rules WHERE rule_id LIKE 'policy-check-%'; "
        "DELETE FROM idempotency_keys WHERE user_id LIKE 'policy-check-%'; "
        "ALTER TABLE policy_results ENABLE TRIGGER policy_results_never_change; "
        "ALTER TABLE policy_rule_versions ENABLE TRIGGER policy_rule_versions_never_change; "
        "SELECT 1"
    )
    cases.clean_up()


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        bootstrap(TENANT, AUDITOR, "auditor")
        bootstrap(OTHER, STRANGER, "reliability-engineer")
        with (
            port_forward("deployment/windops-investigation", APPLICATION, 50051) as case_port,
            blocking_channel(case_port, CASE_HOST, bundle) as case_channel,
            port_forward("deployment/windops-policy", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            investigation = Caller(
                investigation_pb2_grpc.InvestigationServiceStub(case_channel), key
            )
            policy = Caller(policy_pb2_grpc.PolicyServiceStub(channel), key)
            case_id = open_case(investigation)
            result = check_judgements(policy, case_id)
            check_site_limit(policy, case_id)
            check_reads(policy, result)
        check_audit(bundle, key, result)
        check_database(result)
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Policy check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
