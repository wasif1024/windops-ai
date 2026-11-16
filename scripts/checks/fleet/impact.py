#!/usr/bin/env python3
"""Prove the Production Impact Service on the cluster, and Policy judging its estimate (M6.2).

Run the platform and the simulator first, as `make dev` does. A Control Room Operator opens a
check-only case on a demo turbine, which the simulator streams live, and the check proves:

- a tenant starts with the defaults, impact-references-1, readable with policy.read: the demo
  models' power curves, the energy price and the maintenance costs;
- an estimate for a repair gathers the turbine, its model and its site from Asset and six hours
  of wind and power from Telemetry, as the service itself, and keeps every input, the formula
  version, its assumptions and a low to high range for each figure;
- a Tenant Administrator, with a recent second factor, gives the site a price of its own, and
  the next estimate there uses it; a password alone is refused;
- an estimate reads back verified, and another tenant cannot find it;
- the Policy Service judges the case's proposal on that estimate: its cost limit escalates on the
  high end of the estimate's range, and without an estimate, cost and downtime escalate;
- PostgreSQL refuses to change or delete an estimate or a reference version, and row-level
  security hides them from another tenant.

Its case, references and estimates are removed at the end.
"""

import json
import re
import secrets
import subprocess
import sys
from typing import Any

import grpc
from windops.investigation.v1 import investigation_pb2_grpc
from windops.policy.v1 import policy_pb2, policy_pb2_grpc
from windops.production_impact.v1 import production_impact_pb2 as pb
from windops.production_impact.v1 import production_impact_pb2_grpc
from windops_auth import LEVEL_PASSWORD

from scripts.checks.fleet import policy as policy_check
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import kubectl
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-production-impact.windops-application.svc.cluster.local"
POLICY_HOST = "windops-policy.windops-application.svc.cluster.local"
CASE_HOST = "windops-investigation.windops-application.svc.cluster.local"
DATABASE = "windops_production_impact"
TENANT, OTHER, SITE = "north-sea", "atlas-wind", "dogger-east"
# The check opens its case as the policy check's operator, with the same clean-up.
ADMIN, ENGINEER = policy_check.ADMIN, policy_check.ENGINEER
STRANGER = policy_check.STRANGER
RUN = secrets.token_hex(3)
# The service's own minimum of usable telemetry (impact-formulas-1): less, and there is no estimate.
LEAST_SECONDS = 600
PRICE_KEY = f"impact-check-{RUN}-price"
check = policy_check.check


def psql(statement: str, tenant: str = "") -> subprocess.CompletedProcess[str]:
    """As the Production Impact Service's own login, in one tenant, or across them all."""
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


def estimate(impact: Caller, case_id: str, key: str = "") -> Any:
    request = pb.EstimateImpactRequest(case_id=case_id, action="repair", downtime_hours=24.0,
                                       idempotency_key=key)  # fmt: skip
    return impact.call("EstimateImpact", request, ENGINEER, TENANT).result


def figure(result: Any, name: str) -> Any:
    return next(f for f in result.figures if f.name == name)


def check_estimates(impact: Caller, case_id: str) -> Any:
    listed = impact.call("ListReferences", pb.ListReferencesRequest(), ADMIN, TENANT)
    kinds = sorted({r.current.kind for r in listed.references})
    check(
        listed.library == "impact-references-1"
        and kinds == ["energy_price", "maintenance_costs", "power_curve"],
        f"The tenant has the defaults, {listed.library}: {len(listed.references)} references",
    )
    result = estimate(impact, case_id, key=f"impact-check-{RUN}-1")
    inputs = {i.name: i for i in result.inputs}
    total = figure(result, "total_cost_eur")
    check(
        result.verified and result.formula_version == "impact-formulas-1"
        and inputs["turbine.model"].source == "asset"
        and inputs["observed.mean_wind_mps"].source == "telemetry"
        and json.loads(inputs["observed.seconds"].value_json) >= LEAST_SECONDS
        and total.low <= total.value <= total.high and len(result.assumptions) >= 6,
        f"A repair's estimate rests on {json.loads(inputs['observed.seconds'].value_json)} s of "
        f"wind and power, at {json.loads(inputs['observed.mean_wind_mps'].value_json)} m/s: "
        f"{total.value:,.0f} EUR, from {total.low:,.0f} to {total.high:,.0f}",
    )  # fmt: skip
    again = estimate(impact, case_id, key=f"impact-check-{RUN}-1")
    check(again.result_id == result.result_id, "A retry returns the first estimate, not a second")
    return result


def check_site_price(impact: Caller, case_id: str) -> None:
    current = impact.call("GetReference", pb.GetReferenceRequest(reference_id="energy-price"),
                          ADMIN, TENANT).reference.current  # fmt: skip
    definition = json.loads(current.definition_json)
    definition["sites"] = {**definition.get("sites", {}), SITE: 150.0}
    request = pb.PublishReferenceVersionRequest(
        reference_id="energy-price", definition_json=json.dumps(definition),
        reason="Impact check: a site's own price", expected_version=current.version,
        idempotency_key=PRICE_KEY,
    )  # fmt: skip
    refused = impact.code("PublishReferenceVersion", request, ADMIN, TENANT, level=LEVEL_PASSWORD)
    impact.call("PublishReferenceVersion", request, ADMIN, TENANT)
    priced = estimate(impact, case_id)
    revenue, lost = figure(priced, "lost_revenue_eur"), figure(priced, "projected_lost_energy_mwh")
    check(
        refused == grpc.StatusCode.PERMISSION_DENIED
        and abs(revenue.value - lost.value * 150.0) < 0.05,
        f"{SITE} has a price of its own, published with a second factor, and the next estimate "
        "there uses it",
    )


def check_reads(impact: Caller, result: Any) -> None:
    get = pb.GetImpactResultRequest(result_id=result.result_id)
    read = impact.call("GetImpactResult", get, ENGINEER, TENANT).result
    check(read.verified and read.content_hash == result.content_hash,
          "The estimate reads back unchanged, and its hash verifies")  # fmt: skip
    check(impact.code("GetImpactResult", get, STRANGER, OTHER) == grpc.StatusCode.NOT_FOUND,
          "Another tenant cannot find it")  # fmt: skip


def check_policy(policy: Caller, case_id: str, result: Any) -> None:
    proposal = policy_check.sound()

    def judged(impact_id: str) -> Any:
        request = policy_pb2.EvaluateProposalRequest(case_id=case_id, proposal=proposal,
                                                     impact_result_id=impact_id)  # fmt: skip
        return policy.call("EvaluateProposal", request, ENGINEER, TENANT).result

    on_it = judged(result.result_id)
    cost = policy_check.outcome_of(on_it, "cost-limit")
    total = {i.name: i for i in on_it.inputs}["impact.total_cost_eur"]
    high = figure(result, "total_cost_eur").high
    check(
        total.source == "production-impact" and total.source_ref == result.result_id
        and json.loads(total.value_json) == high
        and cost.outcome == ("escalate" if high > 50000 else "pass"),
        f"Policy judges the cost on the estimate's high end, {high:,.0f} EUR: {cost.outcome}",
    )  # fmt: skip
    without = judged("")
    check(
        policy_check.outcome_of(without, "cost-limit").outcome == "escalate"
        and policy_check.outcome_of(without, "downtime-limit").outcome == "escalate",
        "Without an estimate, cost and downtime escalate",
    )


def check_database(result: Any) -> None:
    if not re.fullmatch(r"imp_[a-z0-9]{20}", result.result_id):
        raise RuntimeError(f"Unexpected result ID: {result.result_id!r}")
    for statement in (
        f"UPDATE impact_results SET content = '{{}}' WHERE result_id = '{result.result_id}'",  # noqa: S608  # The ID is checked above.
        f"DELETE FROM impact_results WHERE result_id = '{result.result_id}'",  # noqa: S608
        "UPDATE impact_reference_versions SET reason = 'changed' WHERE reference_id = "
        "'energy-price'",
    ):
        refused = psql(statement, TENANT)
        check(refused.returncode != 0 and "never changes" in refused.stderr,
              f"PostgreSQL refuses: {statement.split(' WHERE')[0]}")  # fmt: skip
    hidden = sql(f"SELECT count(*) FROM impact_results WHERE result_id = '{result.result_id}'",  # noqa: S608
                 OTHER)  # fmt: skip
    check(hidden == "0", "Row-level security hides the estimate from another tenant")


def clean_up() -> None:
    """The check's estimates, and the price version it published, so the tenant's price is the
    one it had. Its case goes with the policy check's clean-up."""
    published = sql(
        "SELECT coalesce(max(version), 0) FROM impact_reference_versions WHERE reference_id = "
        "'energy-price' AND reason = 'Impact check: a site''s own price'", TENANT,
    )  # fmt: skip
    statements = [
        "ALTER TABLE impact_results DISABLE TRIGGER impact_results_never_change",
        "ALTER TABLE impact_reference_versions DISABLE TRIGGER "
        "impact_reference_versions_never_change",
        "DELETE FROM impact_results WHERE estimated_by LIKE 'policy-check-%'",
        "DELETE FROM idempotency_keys WHERE user_id LIKE 'policy-check-%'",
    ]
    if published not in ("", "0"):
        statements += [
            "DELETE FROM impact_reference_versions WHERE reference_id = 'energy-price' AND "
            "reason = 'Impact check: a site''s own price'",
            "UPDATE impact_references SET current_version = (SELECT max(version) FROM "
            "impact_reference_versions v WHERE v.tenant_id = impact_references.tenant_id AND "
            "v.reference_id = 'energy-price') WHERE reference_id = 'energy-price'",
        ]
    statements += [
        "ALTER TABLE impact_results ENABLE TRIGGER impact_results_never_change",
        "ALTER TABLE impact_reference_versions ENABLE TRIGGER "
        "impact_reference_versions_never_change",
        "SELECT 1",
    ]
    sql("; ".join(statements), TENANT)
    policy_check.clean_up()


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        for user, role in ((ADMIN, "tenant-administrator"), (ENGINEER, "reliability-engineer"),
                           (policy_check.OPERATOR, "control-room-operator")):  # fmt: skip
            bootstrap(TENANT, user, role)
        bootstrap(OTHER, STRANGER, "reliability-engineer")
        with (
            port_forward("deployment/windops-investigation", APPLICATION, 50051) as case_port,
            blocking_channel(case_port, CASE_HOST, bundle) as case_channel,
            port_forward("deployment/windops-production-impact", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
            port_forward("deployment/windops-policy", APPLICATION, 50051) as policy_port,
            blocking_channel(policy_port, POLICY_HOST, bundle) as policy_channel,
        ):
            investigation = Caller(
                investigation_pb2_grpc.InvestigationServiceStub(case_channel), key
            )
            impact = Caller(production_impact_pb2_grpc.ProductionImpactServiceStub(channel), key)
            policy = Caller(policy_pb2_grpc.PolicyServiceStub(policy_channel), key)
            case_id = policy_check.open_case(investigation)
            result = check_estimates(impact, case_id)
            check_site_price(impact, case_id)
            check_reads(impact, result)
            check_policy(policy, case_id, result)
        check_database(result)
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Impact check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
