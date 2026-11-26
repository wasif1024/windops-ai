#!/usr/bin/env python3
"""Prove the Phase 10 exit gate: the full flow from a fault to verification, with no approval
bypassed (SRS section 12, M10.7).

On a turbine of its own, in the `asset-check` tenant, the check acts as the turbine's device and
sends its telemetry through the gateway, a sample a second. Then, with the real services:

1. **The fault:** the gearbox oil runs at 85 °C, and the default library's `gearbox-oil-critical`
   rule detects it. The detection opens a critical case, its evidence is frozen, and the
   investigation runs as the tenant's fault-investigator, rehearsed, to `pending_review`.
2. **The proposal:** a Maintenance Planner, who is also a Maintenance Manager, submits a work
   order for it: the agent's draft from step 12 if there is one, re-planned, or their own.
3. **The decision:** a Maintenance Manager approves it on the current evidence, and the decision
   resumes the case's workflow: the case is approved.
4. **The work:** the oil cools to 50 °C, the planner records the work done, and after the
   verification window the outcome is the one the fault rules give.
5. **The close:** the engineer reads the outcome and closes the case.

At each step, every way around the approval is tried, and the gate counts what got through:
- an agent or a service approving, or a person who submitted the work deciding on it (RBAC-004);
- a decision on an older version or snapshot (EVD-009);
- an agent submitting or completing the work, and completing it before it is approved.
The gate passes with 0 of them through.

The investigation is rehearsed, so nothing is asked of Claude. Run `make services` and
`make gateway-routes` first, with WINDOPS_INVESTIGATION_AGENTS=rehearse. Needs local port 9443 free.
About 10 minutes. What it makes is removed at the end.
"""

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from google.protobuf.timestamp_pb2 import Timestamp
from windops.approval.v1 import approval_pb2 as apr
from windops.approval.v1 import approval_pb2_grpc
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.investigation.v1 import investigation_pb2 as inv
from windops.investigation.v1 import investigation_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2 as mnt
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops_investigation.verification import Finding, judge

from scripts.checks.agents.tools import INVESTIGATION_HOST
from scripts.checks.fleet import approval as approval_check
from scripts.checks.fleet import fault_rules
from scripts.checks.fleet.asset import HOST as ASSET_HOST
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.data_quality import SAMPLES, device_token
from scripts.checks.fleet.detection import HOST as FAULT_DETECTION_HOST
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.fleet.telemetry import as_tenant
from scripts.checks.identity.agent_identity import as_service, as_user, code_of
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

RUN = str(int(time.time()))
TENANT, FARM, TURBINE = "asset-check", "full-flow-farm", "full-flow-t01"
ADMIN, ENGINEER = "full-flow-admin", "full-flow-engineer"
# Plans and approves: the separation of duties keeps them off their own work (RBAC-004).
PLANNER, MANAGER = "full-flow-planner", "full-flow-manager"
AGENT = "agent:maintenance-planner"
FAULT_INVESTIGATOR = "service:fault-investigator"
RULE = "gearbox-oil-critical"
REFUSED = (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.FAILED_PRECONDITION,
           grpc.StatusCode.INVALID_ARGUMENT)  # fmt: skip


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def until(what: str, probe: Callable[[], Any], timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(5)
    raise RuntimeError(f"Timed out waiting for {what}")


def stamp(value: datetime) -> Timestamp:
    made = Timestamp()
    made.FromDatetime(value)
    return made


class Device(threading.Thread):
    """The turbine's device: a sample a second, sent five at a time, its gearbox oil as set."""

    def __init__(self, token: str) -> None:
        super().__init__(daemon=True)
        self.token, self.oil, self.stop, self.sent, self.error = (
            token,
            85.0,
            threading.Event(),
            0,
            "",
        )

    def sample(self, second: int) -> dict[str, Any]:
        return {
            "schema_version": "2", "tenant_id": TENANT, "turbine_id": TURBINE,
            "event_time": datetime.fromtimestamp(second, UTC).isoformat(), "sequence": second,
            "source": "full-flow-check", "model_version": "check", "synthetic": True,
            "operating_state": "producing", "available": True, "power_kw": 1500.0,
            "wind_speed_mps": 9.0, "wind_direction_deg": 240.0, "rotor_speed_rpm": 12.0,
            "generator_speed_rpm": 1200.0, "pitch_angle_deg": 2.0, "ambient_temp_c": 10.0,
            "gearbox_oil_temp_c": self.oil, "gearbox_bearing_temp_c": 55.0,
            "main_bearing_temp_c": 35.0, "generator_bearing_temp_c": 50.0,
            "nacelle_vibration_mm_s": 1.2,
        }  # fmt: skip

    def run(self) -> None:
        last = int(time.time()) - 1
        while not self.stop.is_set():
            now = int(time.time())
            batch = [self.sample(second) for second in range(last + 1, now + 1)]
            last = now
            try:
                answer = httpx.post(SAMPLES, json={"samples": batch}, timeout=15,
                                    headers={"Authorization": f"Bearer {self.token}"})  # fmt: skip
                if answer.status_code == 200:
                    self.sent += len(batch)
                else:
                    self.error = f"{answer.status_code}: {answer.text[:200]}"
            except httpx.HTTPError as error:
                self.error = type(error).__name__
            self.stop.wait(5)


class Bench:
    def __init__(self, stack: ExitStack, key: str) -> None:
        bundle = client_bundle()
        self.key, self.case_id = key, ""
        self.attempts: list[tuple[str, grpc.StatusCode]] = []

        def channel(deployment: str, host: str) -> Any:
            port = stack.enter_context(
                port_forward(f"deployment/{deployment}", "windops-application", 50051)
            )
            return stack.enter_context(blocking_channel(port, host, bundle))

        self.assets = Caller(asset_pb2_grpc.AssetServiceStub(
            channel("windops-asset", ASSET_HOST)), key)  # fmt: skip
        self.rules = Caller(fault_detection_pb2_grpc.FaultRuleServiceStub(
            channel("windops-fault-detection", FAULT_DETECTION_HOST)), key)  # fmt: skip
        self.investigation = Caller(investigation_pb2_grpc.InvestigationServiceStub(
            channel("windops-investigation", INVESTIGATION_HOST)), key)  # fmt: skip
        self.maintenance_raw = maintenance_pb2_grpc.MaintenanceServiceStub(
            channel("windops-maintenance", MAINTENANCE_HOST))  # fmt: skip
        self.maintenance = Caller(self.maintenance_raw, key)
        self.approvals_raw = approval_pb2_grpc.ApprovalServiceStub(
            channel("windops-approval", approval_check.HOST))  # fmt: skip
        self.approvals = Caller(self.approvals_raw, key)

    def case(self) -> Any:
        return self.investigation.call("GetCase", inv.GetCaseRequest(case_id=self.case_id),
                                       ENGINEER, TENANT)  # fmt: skip

    def work_order(self, work_order_id: str) -> Any:
        return self.maintenance.call("GetWorkOrder", mnt.GetWorkOrderRequest(
            work_order_id=work_order_id), PLANNER, TENANT)  # fmt: skip

    def attempt(self, what: str, call: Callable[[], Any]) -> None:
        """A way around the approval: it must be refused."""
        code = code_of(call)
        self.attempts.append((what, code))
        if code not in REFUSED:
            raise RuntimeError(f"Failed: {what} was not refused: {code.name}")

    def bypassed(self) -> list[str]:
        return [what for what, code in self.attempts if code not in REFUSED]


def make_turbine(bench: Bench) -> None:
    for method, request in (
        ("CreateWindFarm", asset_pb2.CreateWindFarmRequest(
            wind_farm_id=FARM, name=FARM, country="GB", time_zone="Europe/London",
            location=asset_pb2.Location(latitude=54.0, longitude=1.0), offshore=True)),
        ("CreateTurbine", asset_pb2.CreateTurbineRequest(
            turbine_id=TURBINE, wind_farm_id=FARM, name="Full flow check T01",
            location=asset_pb2.Location(latitude=54.0, longitude=1.0),
            model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic", model="check"))),
    ):  # fmt: skip
        code = bench.assets.code(method, request, ADMIN, TENANT)
        if code not in (grpc.StatusCode.OK, grpc.StatusCode.ALREADY_EXISTS):
            raise RuntimeError(f"{method} answered {code.name}")


def fault(bench: Bench, device: Device) -> Any:
    """Step 1: the fault is detected, opens a case, and is investigated to pending review."""
    until("the device's telemetry", lambda: device.sent >= 10, 60)
    if device.error:
        raise RuntimeError(f"The device route failed: {device.error}")

    def opened() -> Any:
        fault_rules.last_line(fault_rules.sql(
            "UPDATE turbine_watch SET last_evaluated_at = NULL "  # noqa: S608
            f"WHERE turbine_id = '{TURBINE}'; SELECT 1", tenant=TENANT,
        ))  # fmt: skip
        listed = bench.investigation.call(
            "ListCases", inv.ListCasesRequest(turbine_id=TURBINE), ENGINEER, TENANT
        ).cases
        return next((c for c in listed if c.severity == "critical"), None)

    case = until("a critical case on the turbine", opened, 300)
    bench.case_id = case.case_id
    got = bench.case()
    rules = {d.rule_id for d in got.detections}
    check(case.origin == "detection" and RULE in rules,
          f"The seeded fault, gearbox oil at 85 °C, is detected by {RULE} and opens critical "
          f"case {case.case_id}")  # fmt: skip

    def waiting() -> Any:
        found = bench.case().case
        return found if found.status == "pending_review" else None

    reviewed = until("the investigation to reach pending review", waiting, 600)
    check(bool(reviewed.evidence_snapshot_id) and reviewed.investigation_status == "completed",
          f"Its evidence is frozen ({reviewed.evidence_snapshot_id}) and the investigation, "
          f"run {reviewed.run_id}, waits for review (SRS 5.4 step 13)")  # fmt: skip
    return reviewed


def plan(procedures: Any = ()) -> mnt.Plan:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    return mnt.Plan(
        title=f"Full flow check {RUN}: gearbox oil cooling", action="inspection",
        summary="Inspect the gearbox oil cooler's thermostatic valve, from the ground panel.",
        tasks=[mnt.Task(description="Inspect the thermostatic valve", duration_hours=2,
                        crew_size=2, requires_climb=False)],
        procedures=list(procedures),
        planned_start=stamp(start), planned_end=stamp(start + timedelta(hours=3)),
        estimated_downtime_hours=3,
    )  # fmt: skip


def propose(bench: Bench) -> Any:
    """Step 2: the planner submits the agent's draft, re-planned, or a draft of their own. An
    agent may never submit it."""
    drafts = bench.maintenance.call("ListWorkOrders", mnt.ListWorkOrdersRequest(
        case_id=bench.case_id, statuses=["draft"]), PLANNER, TENANT).work_orders  # fmt: skip
    agents = [d for d in drafts if d.drafted_by_agent]
    if agents:
        update = mnt.UpdateDraftRequest(
            work_order_id=agents[0].work_order_id, expected_revision=agents[0].revision,
            plan=plan(agents[0].plan.procedures), idempotency_key=f"ffc-{RUN}-update",
        )  # fmt: skip
        draft = bench.maintenance.call("UpdateDraft", update, PLANNER, TENANT).work_order
        print(f"info - The agent drafted {draft.work_order_id} in step 12; the planner "
              "re-plans it", flush=True)  # fmt: skip
    else:
        draft = bench.maintenance.call("CreateDraft", mnt.CreateDraftRequest(
            case_id=bench.case_id, plan=plan(), idempotency_key=f"ffc-{RUN}"),
            PLANNER, TENANT).work_order  # fmt: skip
    submit = mnt.SubmitWorkOrderRequest(work_order_id=draft.work_order_id,
                                        expected_revision=draft.revision,
                                        idempotency_key=f"ffc-{RUN}-submit")  # fmt: skip
    bench.attempt("the planner's agent submits it", lambda: bench.maintenance_raw.SubmitWorkOrder(
        submit, **as_user(PLANNER, bench.key, TENANT, actor=AGENT)))  # fmt: skip
    principal = as_service(FAULT_INVESTIGATOR, bench.key, TENANT)
    bench.attempt("the fault-investigator submits it",
                  lambda: bench.maintenance_raw.SubmitWorkOrder(submit, **principal))  # fmt: skip
    order = bench.maintenance.call("SubmitWorkOrder", submit, PLANNER, TENANT).work_order
    check(order.status == "submitted" and order.submitted_by == PLANNER,
          f"The planner submits work order {order.work_order_id}; neither the agent nor the "
          "fault-investigator may")  # fmt: skip
    return order


def requested(bench: Bench, work_order_id: str) -> Any:
    def found() -> Any:
        listed = bench.approvals.call("ListApprovals", apr.ListApprovalsRequest(
            work_order_id=work_order_id), MANAGER, TENANT).approvals  # fmt: skip
        return next((a for a in listed if a.status == "pending"), None)

    return until("the approval request", found, 120)


def decide(bench: Bench, order: Any) -> Any:
    """Step 3: only a person who had no hand in it approves, on what is current."""
    request = requested(bench, order.work_order_id)
    decision = approval_check.decide(request, reason=f"Full flow check {RUN}: the valve fits",
                                     idempotency_key=f"ffc-{RUN}-decide")  # fmt: skip
    raw = bench.approvals_raw
    bench.attempt("the manager's agent approves", lambda: raw.DecideApproval(
        decision, **as_user(MANAGER, bench.key, TENANT, actor=AGENT)))  # fmt: skip
    bench.attempt("the fault-investigator approves", lambda: raw.DecideApproval(
        decision, **as_service(FAULT_INVESTIGATOR, bench.key, TENANT)))  # fmt: skip
    bench.attempt("the planner who submitted it approves (RBAC-004)", lambda: raw.DecideApproval(
        decision, **as_user(PLANNER, bench.key, TENANT)))  # fmt: skip
    older = approval_check.decide(request, expected_proposal_version=request.proposal_version + 1)
    bench.attempt("a decision on another version (EVD-009)", lambda: raw.DecideApproval(
        older, **as_user(MANAGER, bench.key, TENANT)))  # fmt: skip
    elsewhere = approval_check.decide(request, expected_evidence_snapshot_id="evs_" + "0" * 20)
    bench.attempt("a decision on another snapshot (EVD-009)", lambda: raw.DecideApproval(
        elsewhere, **as_user(MANAGER, bench.key, TENANT)))  # fmt: skip
    complete = mnt.CompleteWorkOrderRequest(
        work_order_id=order.work_order_id, expected_revision=order.revision,
        completion=completion(datetime.now(UTC) - timedelta(minutes=1)),
    )  # fmt: skip
    bench.attempt("the planner completes it before it is approved", lambda: bench.maintenance_raw
                  .CompleteWorkOrder(complete, **as_user(PLANNER, bench.key, TENANT)))  # fmt: skip
    still = bench.approvals.call("GetApproval", apr.GetApprovalRequest(
        approval_id=request.approval_id), MANAGER, TENANT).approval  # fmt: skip
    check(still.status == "pending", f"{len(bench.attempts)} ways around the approval are "
          "refused, and the request still waits for a decision")  # fmt: skip
    bench.approvals.call("DecideApproval", decision, MANAGER, TENANT)
    approved = until("the case to be approved",
                     lambda: (c := bench.case().case).status == "approved" and c, 300)  # fmt: skip
    got = until("the work order to be approved",
                lambda: (w := bench.work_order(order.work_order_id)).work_order.status
                == "approved" and w, 120)  # fmt: skip
    check(approved.evidence_snapshot_id == request.evidence_snapshot_id,
          f"The manager approves it on snapshot {request.evidence_snapshot_id}; the decision "
          "resumes the case's workflow, and the case is approved (SRS 5.4 step 14)")  # fmt: skip
    return got


def completion(started: datetime, ended: datetime | None = None) -> mnt.Completion:
    return mnt.Completion(
        started_at=stamp(started), completed_at=stamp(ended or started + timedelta(seconds=30)),
        summary="Replaced the thermostatic valve; the oil cooled under load",
        actual_downtime_hours=0.5,
    )  # fmt: skip


def work(bench: Bench, device: Device, got: Any) -> Any:
    """Step 4: the work is recorded done by a person, and verified by the rules."""
    order = got.work_order
    approved_at = [e for e in got.history if e.action == "approved"][-1].at.ToDatetime(tzinfo=UTC)
    device.oil = 50.0
    time.sleep(10)
    request = mnt.CompleteWorkOrderRequest(
        work_order_id=order.work_order_id, expected_revision=order.revision,
        completion=completion(approved_at + timedelta(seconds=1), datetime.now(UTC)),
        idempotency_key=f"ffc-{RUN}-done",
    )  # fmt: skip
    agent = as_user(PLANNER, bench.key, TENANT, actor=AGENT)
    bench.attempt("the planner's agent completes it",
                  lambda: bench.maintenance_raw.CompleteWorkOrder(request, **agent))  # fmt: skip
    done = bench.maintenance.call("CompleteWorkOrder", request, PLANNER, TENANT).work_order
    check(
        done.status == "completed",
        "The oil cools to 50 °C, and the planner records the work done (FR-MNT-004)",
    )
    verified = until("the verification",
                     lambda: (c := bench.case()).case.status == "verified" and c, 600)  # fmt: skip
    [report] = verified.verifications
    again = bench.rules.call("EvaluateTurbine", fd.EvaluateTurbineRequest(
        turbine_id=TURBINE, at=report.window_end), ENGINEER, TENANT)  # fmt: skip
    case_rules = {d.rule_id for d in verified.detections}
    expected, _, _ = judge(int(report.after_samples), [
        Finding(f.rule_id, f.version, f.kind, f.severity, f.summary) for f in again.findings
    ], case_rules)  # fmt: skip
    oil = next((m for m in report.metrics if m.metric == "METRIC_GEARBOX_TEMPERATURE"), None)
    check(report.outcome == expected and oil is not None and oil.after_mean < oil.before_mean,
          f"After the window the work is verified {report.outcome}, as the rules give: gearbox "
          f"oil {oil.before_mean if oil else '?'} then {oil.after_mean if oil else '?'} °C "
          "(SRS 5.4 step 15, FR-MNT-005)")  # fmt: skip
    return verified


def close(bench: Bench, verified: Any) -> None:
    """Step 5: a person answers the report."""
    answer = inv.ReviewVerificationRequest(case_id=bench.case_id, decision="close",
                                           expected_version=verified.case.version,
                                           idempotency_key=f"ffc-{RUN}-close")  # fmt: skip
    closed = bench.investigation.call("ReviewVerification", answer, ENGINEER, TENANT)
    check(closed.case.status == "closed", "The engineer reads the outcome and closes the case")
    bypassed = bench.bypassed()
    check(not bypassed, f"0 approval bypasses: all {len(bench.attempts)} attempts were refused: "
          + "; ".join(f"{what} ({code.name})" for what, code in bench.attempts))  # fmt: skip


def clean_up(bench: Bench | None, device: Device | None) -> None:
    if device is not None:
        device.stop.set()
    if bench is None:
        return
    listed = bench.investigation.call("ListCases", inv.ListCasesRequest(turbine_id=TURBINE),
                                      ENGINEER, TENANT).cases  # fmt: skip
    for case in listed:
        bench.investigation.code("CancelCase", inv.CancelCaseRequest(
            case_id=case.case_id, reason="Check over"), ENGINEER, TENANT)  # fmt: skip
    mine = sorted(c for c in {bench.case_id, *(c.case_id for c in listed)} if c)
    approval_check.clean_up(mine)
    where = f"tenant_id = '{TENANT}' AND turbine_id = '{TURBINE}'"
    as_tenant(TENANT, (
        f"DELETE FROM operational_samples WHERE {where}; "  # noqa: S608
        f"UPDATE device_credentials SET revoked_at = now() WHERE {where} AND revoked_at IS NULL; "
        "SELECT 1"
    ))  # fmt: skip


def main() -> int:
    bench: Bench | None = None
    device: Device | None = None
    try:
        key = signing_key()
        for user, role in ((ADMIN, "tenant-administrator"), (ENGINEER, "reliability-engineer"),
                           (PLANNER, "maintenance-planner"), (PLANNER, "maintenance-manager"),
                           (MANAGER, "maintenance-manager")):  # fmt: skip
            bootstrap(TENANT, user, role)
        with ExitStack() as stack:
            stack.enter_context(keycloak_public())
            bench = Bench(stack, key)
            stack.callback(lambda: clean_up(bench, device))
            make_turbine(bench)
            device = Device(device_token(TENANT, TURBINE))
            device.start()
            fault(bench, device)
            order = propose(bench)
            got = decide(bench, order)
            verified = work(bench, device, got)
            close(bench, verified)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Full flow check passed")
    return 0


if __name__ == "__main__":
    assert KUBE_CONTEXT  # noqa: S101
    sys.exit(main())
