#!/usr/bin/env python3
"""Prove step 15 on the cluster: completed work is verified, and a person answers it (M10.6).

An engineer opens a case on a demo turbine, and its evidence is frozen. A Maintenance Planner
drafts and submits a work order, and a Maintenance Manager approves it. Then, with the real
services:

- the planner records the work done (FR-MNT-004); their agent, and an operator who may not
  complete work, are refused (SEC-009);
- workorder.completed reaches the Investigation Service, and the case is verifying;
- once the window passes, the telemetry after the work is frozen with its own hash, compared with
  the case's evidence metric by metric, and the fault rules run again at the window's end;
- the outcome is the one the rules give, asked again here (FR-MNT-005); the case is verified,
  the work order verified with it, and the engineer who opened the case is told;
- the planner may not answer it; the engineer closes the case.

The window is WINDOPS_VERIFICATION_WINDOW_MINUTES on the cluster: two minutes locally. Run
`make services` first. What it makes is removed at the end.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from google.protobuf.timestamp_pb2 import Timestamp
from windops.approval.v1 import approval_pb2_grpc
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.investigation.v1 import investigation_pb2 as inv
from windops.investigation.v1 import investigation_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2 as mnt
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops.notification.v1 import notification_pb2 as ntf
from windops.notification.v1 import notification_pb2_grpc
from windops_investigation.verification import Finding, judge

from scripts.checks.agents.tools import INVESTIGATION_HOST, Cases
from scripts.checks.fleet import approval as approval_check
from scripts.checks.fleet import notification as notification_check
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.identity.agent_identity import as_user, code_of
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

TENANT = "north-sea"
ENGINEER, PLANNER = "verification-check-engineer", "verification-check-planner"
MANAGER, OPERATOR = "verification-check-manager", "verification-check-operator"
AGENT = "agent:maintenance-planner"
FAULT_DETECTION_HOST = "windops-fault-detection.windops-application.svc.cluster.local"
RUN = str(int(time.time()))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def until(what: str, probe: Callable[[], Any], timeout: float = 120) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for {what}")


def stamp(value: datetime) -> Timestamp:
    made = Timestamp()
    made.FromDatetime(value)
    return made


class Bench:
    def __init__(self, stack: ExitStack, key: str) -> None:
        bundle = client_bundle()
        self.key, self.closed = key, list[str]()

        def channel(deployment: str, host: str) -> Any:
            port = stack.enter_context(
                port_forward(f"deployment/{deployment}", "windops-application", 50051)
            )
            return stack.enter_context(blocking_channel(port, host, bundle))

        self.maintenance_raw = maintenance_pb2_grpc.MaintenanceServiceStub(
            channel("windops-maintenance", MAINTENANCE_HOST)
        )
        self.maintenance = Caller(self.maintenance_raw, key)
        self.approvals = Caller(approval_pb2_grpc.ApprovalServiceStub(
            channel("windops-approval", approval_check.HOST)), key)  # fmt: skip
        self.investigation = Caller(investigation_pb2_grpc.InvestigationServiceStub(
            channel("windops-investigation", INVESTIGATION_HOST)), key)  # fmt: skip
        self.rules = Caller(fault_detection_pb2_grpc.FaultRuleServiceStub(
            channel("windops-fault-detection", FAULT_DETECTION_HOST)), key)  # fmt: skip
        self.notifications = Caller(notification_pb2_grpc.NotificationServiceStub(
            channel("windops-notification", notification_check.HOST)), key)  # fmt: skip
        self.cases = Cases(key)
        stack.callback(self.cases.close)

    def case(self, case_id: str) -> Any:
        return self.investigation.call("GetCase", inv.GetCaseRequest(case_id=case_id), ENGINEER,
                                       TENANT)  # fmt: skip

    def work_order(self, work_order_id: str) -> Any:
        return self.maintenance.call(
            "GetWorkOrder", mnt.GetWorkOrderRequest(work_order_id=work_order_id), PLANNER, TENANT
        )


def approved(bench: Bench) -> tuple[str, Any]:
    """A case, and its work order once approved and the case moved."""
    case_id = bench.cases.open(ENGINEER, title=f"Verification check {RUN}")
    bench.cases.evidence(case_id)
    order = approval_check.submit(bench.maintenance, case_id, 0, planner=PLANNER)
    [request] = approval_check.requested(bench.approvals, order.work_order_id)
    decision = approval_check.decide(request, idempotency_key=f"vfy-{RUN}")
    bench.approvals.call("DecideApproval", decision, MANAGER, TENANT)
    until("the case to be approved", lambda: bench.case(case_id).case.status == "approved")
    got = until("the work order to be approved",
                lambda: (w := bench.work_order(order.work_order_id)).work_order.status
                == "approved" and w)  # fmt: skip
    return case_id, got


def complete(bench: Bench, got: Any) -> Any:
    order = got.work_order
    approved_at = [e for e in got.history if e.action == "approved"][-1].at.ToDatetime(tzinfo=UTC)
    now = datetime.now(UTC)
    completion = mnt.Completion(
        started_at=stamp(approved_at + timedelta(seconds=1)), completed_at=stamp(now),
        summary="Replaced the gearbox oil filter and checked the bearing temperatures",
        parts_used=[mnt.PartLine(part_number="GBX-FAN-CAP", quantity=1)],
        actual_downtime_hours=1.5,
    )  # fmt: skip
    request = mnt.CompleteWorkOrderRequest(
        work_order_id=order.work_order_id, expected_revision=order.revision,
        completion=completion, idempotency_key=f"vfy-{RUN}-done",
    )  # fmt: skip
    as_agent = as_user(PLANNER, bench.key, TENANT, actor=AGENT)
    refused = [
        code_of(lambda: bench.maintenance_raw.CompleteWorkOrder(request, **as_agent)),
        bench.maintenance.code("CompleteWorkOrder", request, OPERATOR, TENANT),
    ]
    check(refused == [grpc.StatusCode.PERMISSION_DENIED] * 2,
          "The planner's agent, and an operator, may not record work done (SEC-009)")  # fmt: skip
    done = bench.maintenance.call("CompleteWorkOrder", request, PLANNER, TENANT).work_order
    check(done.status == "completed" and done.completion.recorded_by == PLANNER,
          f"The planner records work order {done.work_order_id} done (FR-MNT-004)")  # fmt: skip
    return done


def prove(bench: Bench) -> None:
    case_id, got = approved(bench)
    done = complete(bench, got)
    verifying = until(
        "the case to be verifying",
        lambda: (c := bench.case(case_id)).case.status == "verifying" and c,
    )
    [waiting] = verifying.verifications
    window = waiting.window_end.ToDatetime(tzinfo=UTC) - waiting.window_start.ToDatetime(tzinfo=UTC)
    check(waiting.status == "waiting" and waiting.completed_by == PLANNER,
          f"workorder.completed reached Investigation: it verifies for {window}")  # fmt: skip

    timeout = window.total_seconds() + 240
    reported = until("the verification to be reported",
                     lambda: (c := bench.case(case_id)).case.status == "verified" and c,
                     timeout)  # fmt: skip
    [verified] = reported.verifications
    compared = {m.metric: m for m in verified.metrics}
    bearing = compared.get("METRIC_BEARING_TEMPERATURE")
    check(
        verified.after_telemetry_snapshot_id.startswith("tsnap_")
        and verified.before_telemetry_snapshot_id.startswith("tsnap_")
        and bearing is not None and bearing.HasField("before_mean")
        and bearing.HasField("after_mean"),
        f"The {verified.after_samples} samples after the work are frozen and compared with the "
        f"evidence: bearing {bearing.before_mean if bearing else '?'} then "
        f"{bearing.after_mean if bearing else '?'} {bearing.unit if bearing else ''}",
    )  # fmt: skip

    at = verified.window_end
    again = bench.rules.call("EvaluateTurbine", fd.EvaluateTurbineRequest(
        turbine_id=reported.case.turbine_id, at=at), ENGINEER, TENANT)  # fmt: skip
    findings = [Finding(f.rule_id, f.version, f.kind, f.severity, f.summary)
                for f in again.findings]  # fmt: skip
    expected, _, _ = judge(int(verified.after_samples), findings, set())
    check(
        verified.outcome == expected
        and {r.rule_id for r in verified.rules} == {f.rule_id for f in findings},
        f"The outcome is the rules' own, asked again: {verified.outcome} "
        f"({len(findings)} finding(s) at the window's end) (FR-MNT-005)",
    )  # fmt: skip

    order = until("the work order to be verified",
                  lambda: (w := bench.work_order(done.work_order_id).work_order).status
                  == "verified" and w)  # fmt: skip
    check(order.verification_outcome == verified.outcome,
          f"The work order is verified, {order.verification_outcome}, with the case")  # fmt: skip

    def told() -> Any:
        mine = bench.notifications.call(
            "ListMyNotifications", ntf.ListMyNotificationsRequest(page_size=200), ENGINEER, TENANT
        ).notifications
        return next((n for n in mine if n.case_id == case_id and n.kind == "case_verified"), None)

    note = until("the engineer to be told", told)
    check(note.work_order_id == done.work_order_id and "Replaced" not in note.body,
          f"The engineer who opened the case is told: \"{note.title}\"")  # fmt: skip

    version = reported.case.version
    answer = inv.ReviewVerificationRequest(case_id=case_id, decision="close",
                                           expected_version=version,
                                           idempotency_key=f"vfy-{RUN}-close")  # fmt: skip
    check(bench.investigation.code("ReviewVerification", answer, PLANNER, TENANT)
          == grpc.StatusCode.PERMISSION_DENIED,
          "The planner, who may not review findings, may not answer it")  # fmt: skip
    closed = bench.investigation.call("ReviewVerification", answer, ENGINEER, TENANT)
    bench.cases.opened.remove(case_id)
    bench.closed.append(case_id)
    check(closed.case.status == "closed" and closed.verification.review == "closed",
          "The engineer reads the outcome and closes the case")  # fmt: skip


def main() -> int:
    try:
        key = signing_key()
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, MANAGER, "maintenance-manager")
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        with ExitStack() as stack:
            bench = Bench(stack, key)
            stack.callback(lambda: notification_check.clean_up(bench.cases.opened + bench.closed))
            prove(bench)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Verification check passed")
    return 0


if __name__ == "__main__":
    assert KUBE_CONTEXT  # noqa: S101
    sys.exit(main())
