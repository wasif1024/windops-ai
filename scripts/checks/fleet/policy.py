#!/usr/bin/env python3
"""Prove the Policy Service on the cluster: proposals judged, results recorded for ever (M6.1).

Run the platform first, as `make dev` does. A Control Room Operator opens a check-only case on a
demo turbine, and once its evidence is frozen, the check proves:

- a tenant starts with the default library, policy-rules-1, readable with policy.read;
- a sound proposal passes, with every rule version and input recorded: the case and its evidence
  from the Investigation Service, the turbine and its site from the Asset Service, and the cost
  and downtime from the Production Impact Service's estimate, gathered by the Policy Service as
  itself (ADR 0009);
- work at height is judged on the Weather Service's forecast from Open-Meteo, and blocks with no
  planned window to forecast (FR-POL-004); without an estimate, cost and downtime escalate, as an
  unstated confidence does (FR-POL-005);
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
from windops.production_impact.v1 import production_impact_pb2 as impact_pb2
from windops.production_impact.v1 import production_impact_pb2_grpc as impact_pb2_grpc
from windops_auth import LEVEL_PASSWORD

from scripts.checks.fleet import cases
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-policy.windops-application.svc.cluster.local"
IMPACT_HOST = "windops-production-impact.windops-application.svc.cluster.local"
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


def evaluate(
    policy: Caller, case_id: str, proposal: pb.Proposal, key: str = "", impact: str = ""
) -> Any:
    request = pb.EvaluateProposalRequest(
        case_id=case_id, proposal=proposal, idempotency_key=key, impact_result_id=impact
    )
    return policy.call("EvaluateProposal", request, ENGINEER, TENANT).result


def outcome_of(result: Any, rule_id: str) -> Any:
    return next(r for r in result.rules if r.rule_id == rule_id)


def estimate(impact: Caller, case_id: str) -> str:
    """The Production Impact Service's estimate for a 4-hour inspection, which Policy judges."""
    request = impact_pb2.EstimateImpactRequest(case_id=case_id, action="inspection",
                                               downtime_hours=4.0)  # fmt: skip
    return str(impact.call("EstimateImpact", request, ENGINEER, TENANT).result.result_id)


def check_judgements(policy: Caller, case_id: str, impact_id: str) -> Any:
    listed = policy.call("ListPolicyRules", pb.ListPolicyRulesRequest(), ADMIN, TENANT)
    check(
        listed.library == "policy-rules-2" and len(listed.rules) >= 10
        and all(r.current.created_by == "system" for r in listed.rules if r.rule_id != RULE),
        f"The tenant has the default library, {listed.library}: {len(listed.rules)} rules",
    )  # fmt: skip
    passed = evaluate(policy, case_id, sound(), key=f"policy-check-{RUN}-1", impact=impact_id)
    inputs = {i.name: i for i in passed.inputs}
    check(
        passed.outcome == "pass" and passed.verified and passed.wind_farm_id == SITE
        and inputs["case.severity"].source == "investigation"
        and inputs["case.severity"].source_ref.startswith(f"{case_id}@")
        and inputs["turbine.status"].source == "asset"
        and inputs["evidence.age_hours"].state == "available"
        and inputs["impact.total_cost_eur"].source == "production-impact",
        f"A sound proposal passes, judged by {len(passed.rules)} rule versions on inputs gathered "
        "from Investigation, Asset and Production Impact",
    )  # fmt: skip
    again = evaluate(policy, case_id, sound(), key=f"policy-check-{RUN}-1", impact=impact_id)
    check(again.result_id == passed.result_id, "A retry returns the first result, not a second")
    climb = evaluate(policy, case_id, sound(
        requires_climb=True, planned_start=stamp(datetime.now(UTC) + timedelta(hours=2)),
        planned_end=stamp(datetime.now(UTC) + timedelta(hours=6)),
    ), impact=impact_id)  # fmt: skip
    wind = outcome_of(climb, "work-at-height-wind")
    forecast = {i.name: i for i in climb.inputs}["weather.max_wind_speed_mps"]
    hub = json.loads(forecast.value_json) if forecast.value_json else None
    check(
        forecast.source == "weather" and forecast.source_ref.startswith("open-meteo:fct_")
        and hub is not None and wind.outcome == ("block" if hub > 12.0 else "pass"),
        f"Work at height is judged on Open-Meteo's forecast through the Weather Service: "
        f"{hub} m/s at the hub, so {wind.outcome}",
    )  # fmt: skip
    unplanned = outcome_of(evaluate(policy, case_id, sound(requires_climb=True),
                                    impact=impact_id), "work-at-height-wind")  # fmt: skip
    check(unplanned.outcome == "block" and "no planned window" in unplanned.reason,
          "A climb with no planned window to forecast blocks")  # fmt: skip
    # The caller's own cost is kept, and judges nothing: without an estimate, cost escalates.
    unpriced = evaluate(policy, case_id, sound(estimated_cost_eur=1.0, confidence=None))
    check(
        unpriced.outcome == "escalate"
        and outcome_of(unpriced, "cost-limit").outcome == "escalate"
        and outcome_of(unpriced, "downtime-limit").outcome == "escalate"
        and outcome_of(unpriced, "low-confidence").outcome == "escalate",
        "Without an estimate, cost and downtime escalate, and so does an unstated confidence",
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
    # The estimates it asked for. The Production Impact Service's tables, as its own login.
    estimates = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "-q", "-At",
        "--username=windops_production_impact", "--dbname=windops_production_impact",
        "--set=ON_ERROR_STOP=1", "--command=SELECT set_config('windops.all_tenants', 'on', "
        "false); ALTER TABLE impact_results DISABLE TRIGGER impact_results_never_change; "
        "DELETE FROM impact_results WHERE estimated_by LIKE 'policy-check-%'; ALTER TABLE "
        "impact_results ENABLE TRIGGER impact_results_never_change; DELETE FROM "
        "idempotency_keys WHERE user_id LIKE 'policy-check-%'",
        namespace=DATA,
    )  # fmt: skip
    if estimates.returncode != 0 and "does not exist" not in estimates.stderr:
        raise RuntimeError(f"SQL failed: {estimates.stderr.strip()}")
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
            port_forward("deployment/windops-production-impact", APPLICATION, 50051) as i_port,
            blocking_channel(i_port, IMPACT_HOST, bundle) as impact_channel,
        ):
            investigation = Caller(
                investigation_pb2_grpc.InvestigationServiceStub(case_channel), key
            )
            policy = Caller(policy_pb2_grpc.PolicyServiceStub(channel), key)
            impact = Caller(impact_pb2_grpc.ProductionImpactServiceStub(impact_channel), key)
            case_id = open_case(investigation)
            result = check_judgements(policy, case_id, estimate(impact, case_id))
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
