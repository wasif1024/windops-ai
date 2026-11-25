#!/usr/bin/env python3
"""Prove the stale rules on the cluster (SRS 8.5, EVD-007, EVD-008), with the real services.

A case is opened on a demo turbine and its evidence frozen. A Maintenance Planner submits a work
order with a check-only part, 2 in its wind farm's store. Then:

- the part's stock goes to nothing: the Inventory Service announces it, and the Investigation
  Service's inventory rule makes the case stale, naming the rule;
- the Maintenance Service marks the work order stale with that rule, and the Approval Service
  supersedes its request, saying which rule fired, so it cannot be approved (EVD-007);
- it cannot be revised while the case is stale; an engineer re-evaluates the case, freezing a
  new snapshot, and the planner revises it as version 2 on that snapshot, with version 1 kept as
  it was (EVD-008); version 2 is submitted and opens its own request;
- a high-severity alarm on the turbine then makes the new snapshot stale by the alarm rule.

Run `make services` first. What it makes is removed at the end.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

import grpc
import httpx
from windops.approval.v1 import approval_pb2 as apr
from windops.approval.v1 import approval_pb2_grpc
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.inventory.v1 import inventory_pb2 as inv
from windops.inventory.v1 import inventory_pb2_grpc
from windops.investigation.v1 import investigation_pb2 as investigation
from windops.maintenance.v1 import maintenance_pb2 as mnt
from windops.maintenance.v1 import maintenance_pb2_grpc

from scripts.checks.agents.tools import ENGINEER, TURBINE, Cases
from scripts.checks.fleet import approval as approval_check
from scripts.checks.fleet.alarm import report
from scripts.checks.fleet.alarm import sql as alarm_sql
from scripts.checks.fleet.asset import HOST as ASSET_HOST
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.inventory import HOST as INVENTORY_HOST
from scripts.checks.fleet.inventory import sql as inventory_sql
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.fleet.maintenance import plan
from scripts.checks.fleet.telemetry import device_token, send_to
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

TENANT = "north-sea"
PLANNER, MANAGER = "stale-check-planner", "stale-check-manager"
RUN = str(int(time.time()))
PART = f"STALE-{RUN}"
ALARM_TAG = f"stale-check-{RUN}"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def until(what: str, probe: Callable[[], Any], timeout: float = 120) -> Any:
    """Wait for an event to travel: probe until it gives something, or fail naming `what`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for {what}")


class Bench:
    def __init__(self, stack: ExitStack, key: str) -> None:
        bundle = client_bundle()

        def stub(deployment: str, host: str, make: Any) -> Caller:
            port = stack.enter_context(
                port_forward(f"deployment/{deployment}", "windops-application", 50051)
            )
            return Caller(make(stack.enter_context(blocking_channel(port, host, bundle))), key)

        self.asset = stub("windops-asset", ASSET_HOST, asset_pb2_grpc.AssetServiceStub)
        self.inventory = stub("windops-inventory", INVENTORY_HOST,
                              inventory_pb2_grpc.InventoryServiceStub)  # fmt: skip
        self.maintenance = stub("windops-maintenance", MAINTENANCE_HOST,
                                maintenance_pb2_grpc.MaintenanceServiceStub)  # fmt: skip
        self.approvals = stub("windops-approval", approval_check.HOST,
                              approval_pb2_grpc.ApprovalServiceStub)  # fmt: skip
        self.cases = Cases(key)
        stack.callback(self.cases.close)
        self.farm = ""

    def case(self, case_id: str) -> Any:
        return self.cases._api.call(  # The check's own case, as its own engineer.
            "GetCase", investigation.GetCaseRequest(case_id=case_id), ENGINEER, TENANT
        ).case

    def work_order(self, work_order_id: str) -> Any:
        return self.maintenance.call(
            "GetWorkOrder", mnt.GetWorkOrderRequest(work_order_id=work_order_id), PLANNER, TENANT
        )

    def stock(self, delta: int) -> None:
        self.inventory.call(
            "AdjustStock",
            inv.AdjustStockRequest(part_number=PART, wind_farm_id=self.farm, delta=delta,
                                   reason=f"Stale check {RUN}"),
            PLANNER, TENANT,
        )  # fmt: skip

    def seed_part(self) -> None:
        turbine = self.asset.call(
            "GetTurbine", asset_pb2.GetTurbineRequest(turbine_id=TURBINE), PLANNER, TENANT
        ).turbine
        self.farm = turbine.wind_farm_id
        fits = inv.ModelRef(manufacturer=turbine.model.manufacturer, model=turbine.model.model)
        self.inventory.call(
            "PutPart",
            inv.PutPartRequest(part=inv.Part(part_number=PART, unit="each", fits=[fits],
                                             name="Stale check capacitor",
                                             component_kind="gearbox")),
            PLANNER, TENANT,
        )  # fmt: skip
        self.stock(2)


def submit(bench: Bench, case_id: str) -> Any:
    draft = bench.maintenance.call(
        "CreateDraft",
        mnt.CreateDraftRequest(case_id=case_id, plan=plan_with_part(),
                               idempotency_key=f"stale-{RUN}"),
        PLANNER, TENANT,
    ).work_order  # fmt: skip
    parts = {p.part_number: p.status for p in draft.validation.parts}
    check(draft.validation.passed and parts == {PART: "available"},
          f"The planner's draft with {PART} passes, the part available: {parts}, "
          f"{[p.message for p in draft.validation.problems if p.blocking]}")  # fmt: skip
    return bench.maintenance.call(
        "SubmitWorkOrder",
        mnt.SubmitWorkOrderRequest(work_order_id=draft.work_order_id,
                                   expected_revision=draft.revision,
                                   idempotency_key=f"stale-{RUN}-submit"),
        PLANNER, TENANT,
    ).work_order  # fmt: skip


def plan_with_part() -> mnt.Plan:
    made = plan()
    made.parts.append(mnt.PartLine(part_number=PART, quantity=1))
    return made


def inventory_rule(bench: Bench, case_id: str, order: Any) -> Any:
    [request] = approval_check.requested(bench.approvals, order.work_order_id)
    # The Investigation Service watches the proposal once workorder.submitted reaches it.
    time.sleep(5)
    bench.stock(-2)
    case = until("the inventory rule", lambda: (c := bench.case(case_id)).stale_rule and c)
    check(case.stale_rule == "inventory" and PART in case.stale_detail
          and case.stale_rules_version == "stale-rules-2",
          f"The part's stock gone, the case is stale by {case.stale_rules_version}: "
          f"{case.stale_rule}, {case.stale_detail}")  # fmt: skip
    got = until("the work order to go stale",
                lambda: (w := bench.work_order(order.work_order_id)).work_order.status
                == "stale" and w)  # fmt: skip
    check(
        got.work_order.stale_rule == "inventory" and got.history[-1].action == "stale",
        "The Maintenance Service marks the work order stale, with the rule (EVD-007)",
    )
    refused = bench.approvals.code("DecideApproval", approval_check.decide(request), MANAGER,
                                   TENANT)  # fmt: skip
    kept = bench.approvals.call("GetApproval", apr.GetApprovalRequest(
        approval_id=request.approval_id), MANAGER, TENANT).approval  # fmt: skip
    check(
        refused == grpc.StatusCode.FAILED_PRECONDITION
        and kept.status == "superseded"
        and "stale" in kept.superseded_reason
        and "inventory" in kept.superseded_reason,
        f"It cannot be approved, and says which rule fired: {kept.superseded_reason}",
    )
    return got.work_order


def re_evaluate(bench: Bench, case_id: str, stale: Any) -> Any:
    revise = mnt.ReviseWorkOrderRequest(work_order_id=stale.work_order_id,
                                        expected_revision=stale.revision,
                                        idempotency_key=f"stale-{RUN}-revise")  # fmt: skip
    early = bench.maintenance.code("ReviseWorkOrder", revise, PLANNER, TENANT)
    check(
        early == grpc.StatusCode.FAILED_PRECONDITION,
        "It cannot be revised while the case's evidence is stale",
    )
    old = bench.case(case_id).evidence_snapshot_id
    bench.cases._api.call(
        "CreateEvidenceSnapshot",
        investigation.CreateEvidenceSnapshotRequest(case_id=case_id,
                                                    idempotency_key=f"stale-{RUN}-refreeze"),
        ENGINEER, TENANT,
    )  # fmt: skip
    fresh = bench.case(case_id)
    check(
        fresh.evidence_snapshot_id != old and fresh.stale_rule == "",
        f"An engineer re-evaluates the case: snapshot version {fresh.evidence_version}, fresh",
    )
    bench.stock(2)
    revised = bench.maintenance.call("ReviseWorkOrder", revise, PLANNER, TENANT).work_order
    got = bench.work_order(stale.work_order_id)
    [earlier] = got.earlier_versions
    check(revised.version == 2 and revised.status == "draft"
          and revised.evidence_snapshot_id == fresh.evidence_snapshot_id
          and revised.validation.passed and earlier.version == 1 and earlier.status == "stale"
          and earlier.evidence_snapshot_id == old,
          "The planner revises it as version 2 on the new snapshot, and version 1 is kept as "
          "it was (EVD-008)")  # fmt: skip
    submitted = bench.maintenance.call(
        "SubmitWorkOrder",
        mnt.SubmitWorkOrderRequest(work_order_id=revised.work_order_id,
                                   expected_revision=revised.revision,
                                   idempotency_key=f"stale-{RUN}-submit-2"),
        PLANNER, TENANT,
    ).work_order  # fmt: skip
    requests = until("version 2's request", lambda: [
        a for a in approval_check.requested(bench.approvals, submitted.work_order_id)
        if a.proposal_version == 2])  # fmt: skip
    check(
        requests[0].status == "pending", "Version 2 is submitted, and opens its own pending request"
    )
    return submitted


def alarm_rule(bench: Bench, case_id: str, order: Any) -> None:
    raised = report("raise", ALARM_TAG, datetime.now(UTC).replace(microsecond=0))
    # Through the gateway's device route, as a controller would.
    with keycloak_public():
        answer = send_to("alarms", device_token(TENANT, TURBINE), [{**raised, "severity": "high"}])
    check(answer.status_code == 200, "The turbine raises a high-severity alarm")
    case = until("the alarm rule", lambda: (c := bench.case(case_id)).stale_rule and c)
    check(case.stale_rule == "alarm", f"The new snapshot goes stale by the alarm rule: "
                                      f"{case.stale_detail}")  # fmt: skip
    got = until("version 2 to go stale",
                lambda: (w := bench.work_order(order.work_order_id)).work_order.status
                == "stale" and w)  # fmt: skip
    check(
        got.work_order.version == 2 and got.work_order.stale_rule == "alarm",
        "Version 2 is stale by the alarm rule",
    )


def clean_up(case_ids: list[str]) -> None:
    approval_check.clean_up(case_ids)
    inventory_sql(
        # The check's own part only.
        "ALTER TABLE stock_movements DISABLE TRIGGER stock_movements_never_change; "  # noqa: S608
        f"DELETE FROM stock_movements WHERE part_number = '{PART}'; "
        f"DELETE FROM reservations WHERE part_number = '{PART}'; "
        f"DELETE FROM stock WHERE part_number = '{PART}'; "
        f"DELETE FROM part_models WHERE part_number = '{PART}'; "
        f"DELETE FROM parts WHERE part_number = '{PART}'; "
        "ALTER TABLE stock_movements ENABLE TRIGGER stock_movements_never_change; SELECT 1"
    )
    alarm_sql(
        "DELETE FROM alarm_transitions WHERE alarm_id IN (SELECT alarm_id FROM alarms "  # noqa: S608
        f"WHERE tag = '{ALARM_TAG}'); DELETE FROM alarms WHERE tag = '{ALARM_TAG}'; "
        f"DELETE FROM alarm_reports WHERE tag = '{ALARM_TAG}'; SELECT 1"
    )


def main() -> int:
    try:
        key = signing_key()
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, MANAGER, "maintenance-manager")
        with ExitStack() as stack:
            bench = Bench(stack, key)
            stack.callback(lambda: clean_up(bench.cases.opened))
            bench.seed_part()
            case_id = bench.cases.open(ENGINEER, title=f"Stale check {RUN}")
            bench.cases.evidence(case_id)
            order = submit(bench, case_id)
            stale = inventory_rule(bench, case_id, order)
            revised = re_evaluate(bench, case_id, stale)
            alarm_rule(bench, case_id, revised)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Stale check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
