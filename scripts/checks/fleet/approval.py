#!/usr/bin/env python3
"""Prove the Approval Service on the cluster: people decide, agents never, on what is current.

Two cases are opened on a demo turbine and their evidence frozen. A Maintenance Planner drafts and
submits a work order for each. Then, with the real services:

- each workorder.submitted opens one pending approval request (M10.1);
- no agent decides (FR-APR-001, SEC-009): a Maintenance Manager's agent and the
  fault-investigator's agent are refused by the Approval Service itself, and so is a service;
- a planner, who may not approve, is refused, and another tenant cannot find the request;
- a decision on a version or snapshot the manager did not review is refused;
- with the Maintenance Service down, a decision fails closed and the request stays pending
  (EVD-010);
- the manager approves: the decision records approver, time, policy context, proposal version and
  snapshot (FR-APR-002); approval.decided is published, the Maintenance Service moves the work
  order to approved, and the decision reaches the audit trail; the database refuses to change it;
- the second case's evidence is frozen again, so its request is superseded and cannot be approved
  (FR-APR-003, EVD-009).

Run `make services` first. What it makes is removed at the end.
"""

import json
import subprocess
import sys
import time
from contextlib import ExitStack
from typing import Any

import grpc
from windops.approval.v1 import approval_pb2 as pb
from windops.approval.v1 import approval_pb2_grpc
from windops.investigation.v1 import investigation_pb2
from windops.maintenance.v1 import maintenance_pb2, maintenance_pb2_grpc

from scripts.checks.agents.tools import ENGINEER, Cases
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.maintenance import clean_up as clean_up_work_orders
from scripts.checks.fleet.maintenance import plan
from scripts.checks.fleet.maintenance import sql as maintenance_sql
from scripts.checks.identity.agent_identity import as_service, as_user, code_of
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

TENANT, OTHER = "north-sea", "atlas-wind"
PLANNER, MANAGER = "approval-check-planner", "approval-check-manager"
AGENT = "agent:maintenance-planner"
HOST = "windops-approval.windops-application.svc.cluster.local"
MAINTENANCE_HOST = "windops-maintenance.windops-application.svc.cluster.local"
RUN = str(int(time.time()))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def sql(statement: str, database: str = "windops_approval") -> str:
    return maintenance_sql(statement, database)


def kubectl(*args: str) -> None:
    subprocess.run(["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-application",
                    *args], check=True, capture_output=True, text=True)  # fmt: skip


def submit(maintenance: Caller, case_id: str, n: int) -> Any:
    draft = maintenance.call(
        "CreateDraft",
        maintenance_pb2.CreateDraftRequest(case_id=case_id, plan=plan(),
                                           idempotency_key=f"apr-{RUN}-{n}"),
        PLANNER, TENANT,
    ).work_order  # fmt: skip
    return maintenance.call(
        "SubmitWorkOrder",
        maintenance_pb2.SubmitWorkOrderRequest(work_order_id=draft.work_order_id,
                                               expected_revision=draft.revision,
                                               idempotency_key=f"apr-{RUN}-{n}-submit"),
        PLANNER, TENANT,
    ).work_order  # fmt: skip


def requested(approvals: Caller, work_order_id: str) -> Any:
    """The request workorder.submitted opens, once the relay and consumer have run."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        listed = approvals.call(
            "ListApprovals", pb.ListApprovalsRequest(work_order_id=work_order_id), MANAGER, TENANT
        ).approvals
        if listed:
            return listed
        time.sleep(3)
    raise RuntimeError(f"No approval request was opened for {work_order_id}")


def decide(approval: Any, decision: str = "approve", **changes: Any) -> pb.DecideApprovalRequest:
    request = pb.DecideApprovalRequest(
        approval_id=approval.approval_id, decision=decision,
        reason=f"Approval check {RUN}: fits the findings",
        expected_proposal_version=approval.proposal_version,
        expected_evidence_snapshot_id=approval.evidence_snapshot_id,
    )  # fmt: skip
    for name, value in changes.items():
        setattr(request, name, value)
    return request


def refused_to_agents(approvals: Caller, raw: Any, key: str, approval: Any) -> None:
    for who, label in (
        (as_user(MANAGER, key, actor=AGENT), "a Maintenance Manager's agent"),
        (as_service("service:fault-investigator", key, actor=AGENT),
         "the fault-investigator's agent"),
        (as_service("service:agent-runtime", key), "a service"),
    ):  # fmt: skip
        attempt = code_of(lambda w=who: raw.DecideApproval(decide(approval), **w))
        check(attempt == grpc.StatusCode.PERMISSION_DENIED,
              f"Approving as {label} is refused by the Approval Service itself")  # fmt: skip
    planner = approvals.code("DecideApproval", decide(approval), PLANNER, TENANT)
    check(planner == grpc.StatusCode.PERMISSION_DENIED,
          "A planner, without maintenance.approve, is refused")  # fmt: skip
    hidden = approvals.code("GetApproval", pb.GetApprovalRequest(approval_id=approval.approval_id),
                            MANAGER, OTHER)  # fmt: skip
    check(hidden == grpc.StatusCode.NOT_FOUND, "Another tenant cannot find the request")
    for changed, label in (
        ({"expected_proposal_version": approval.proposal_version + 1}, "a version"),
        ({"expected_evidence_snapshot_id": "evs_" + "0" * 20}, "a snapshot"),
    ):
        stale = approvals.code("DecideApproval", decide(approval, **changed), MANAGER, TENANT)
        check(stale == grpc.StatusCode.FAILED_PRECONDITION,
              f"A decision on {label} the manager did not review is refused")  # fmt: skip


def fails_closed(approvals: Caller, approval: Any) -> None:
    """EVD-010, with the Maintenance Service really gone."""
    kubectl("scale", "deployment/windops-maintenance", "--replicas=0")
    try:
        kubectl("wait", "--for=delete", "pod", "-l", "app.kubernetes.io/name=windops-maintenance",
                "--timeout=90s")  # fmt: skip
        down = approvals.code("DecideApproval", decide(approval), MANAGER, TENANT)
    finally:
        kubectl("scale", "deployment/windops-maintenance", "--replicas=1")
        kubectl("rollout", "status", "deployment/windops-maintenance", "--timeout=180s")
    still = approvals.call("GetApproval", pb.GetApprovalRequest(approval_id=approval.approval_id),
                           MANAGER, TENANT).approval  # fmt: skip
    check(
        down == grpc.StatusCode.UNAVAILABLE and still.status == "pending",
        "With the Maintenance Service down, the decision fails closed and stays pending",
    )


def approves(approvals: Caller, maintenance_port: int, approval: Any, key: str) -> None:
    # Its channel to the Maintenance Service reconnects after the outage; a retry with the same
    # key decides once (GRP-007).
    request, deadline = decide(approval, idempotency_key=f"apr-{RUN}-ok"), time.monotonic() + 90
    while True:
        try:
            decided = approvals.call("DecideApproval", request, MANAGER, TENANT).approval
            break
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.UNAVAILABLE or time.monotonic() > deadline:
                raise
            time.sleep(5)
    d = decided.decision
    check(
        decided.status == "approved" and d.decided_by == MANAGER and d.decided_at.seconds > 0
        and d.proposal_version == approval.proposal_version
        and d.evidence_snapshot_id == approval.evidence_snapshot_id
        and d.policy.policy_outcome in ("pass", "escalate") and d.policy.policy_result_id != ""
        and d.policy.case_status != "",
        f"The manager approves; the decision records approver, time, version "
        f"{d.proposal_version}, snapshot and policy {d.policy.policy_outcome} "
        f"({d.policy.policy_result_id}), case {d.policy.case_status}",
    )  # fmt: skip
    again = approvals.code("DecideApproval", decide(approval, "reject"), MANAGER, TENANT)
    check(again == grpc.StatusCode.FAILED_PRECONDITION, "A decided request is never decided again")
    with blocking_channel(maintenance_port, MAINTENANCE_HOST, client_bundle()) as channel:
        maintenance = Caller(maintenance_pb2_grpc.MaintenanceServiceStub(channel), key)
        deadline, status = time.monotonic() + 90, ""
        while time.monotonic() < deadline:
            got = maintenance.call(
                "GetWorkOrder",
                maintenance_pb2.GetWorkOrderRequest(work_order_id=approval.work_order_id),
                MANAGER, TENANT,
            )  # fmt: skip
            status = got.work_order.status
            if status == "approved":
                break
            time.sleep(3)
        last = got.history[-1]
    check(status == "approved" and (last.action, last.actor_id) == ("approved", MANAGER),
          "approval.decided reached the Maintenance Service: the work order is approved, "
          "its history naming the manager")  # fmt: skip
    changed = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-data", "exec",
         "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet",
         "--username=windops_approval", "--dbname=windops_approval", "--set=ON_ERROR_STOP=1",
         "--command=SELECT set_config('windops.all_tenants', 'on', false); "  # noqa: S608
         f"UPDATE approvals SET decided_by = 'someone-else' WHERE approval_id = "
         f"'{approval.approval_id}'"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    check(
        changed.returncode != 0 and "never changes" in changed.stderr,
        "The database refuses to change a decision, even for the service's own login",
    )


def superseded(approvals: Caller, cases: Cases, approval: Any, case_id: str) -> None:
    """FR-APR-003: the case's evidence frozen again, after the request was opened."""
    newer = cases._api.call(  # The check's own case, as its own engineer.
        "CreateEvidenceSnapshot",
        investigation_pb2.CreateEvidenceSnapshotRequest(case_id=case_id,
                                                        idempotency_key=f"apr-{RUN}-refreeze"),
        ENGINEER, TENANT,
    ).snapshot  # fmt: skip
    # The outage above opened the Approval Service's breaker for Maintenance, which refuses
    # decisions until it closes again (SRS 7.7), 30 seconds on.
    deadline = time.monotonic() + 90
    while True:
        refused = approvals.code("DecideApproval", decide(approval), MANAGER, TENANT)
        if refused != grpc.StatusCode.UNAVAILABLE or time.monotonic() > deadline:
            break
        time.sleep(5)
    kept = approvals.call("GetApproval", pb.GetApprovalRequest(approval_id=approval.approval_id),
                          MANAGER, TENANT).approval  # fmt: skip
    check(refused == grpc.StatusCode.FAILED_PRECONDITION and kept.status == "superseded"
          and newer.snapshot_id in kept.superseded_reason,
          f"After a new snapshot, the request is superseded and cannot be approved: "
          f"{kept.superseded_reason}")  # fmt: skip


def published_and_audited(approval_id: str) -> None:
    deadline, found = time.monotonic() + 60, "0"
    while time.monotonic() < deadline:
        found = sql("SELECT count(*) FROM outbox_events WHERE subject = 'approval.decided' "
                    "AND published_at IS NOT NULL")  # fmt: skip
        if int(found) > 0:
            break
        time.sleep(3)
    check(int(found) > 0, "approval.decided was published")
    statement = (
        # The check's own ID.
        "SELECT json_agg(json_build_object('action', action, 'actor', actor_id, "  # noqa: S608
        "'behalf', on_behalf_of)) FROM audit_records WHERE "
        f"(resource_id = '{approval_id}' OR resource_id = 'maintenance.approve') "
        "AND recorded_at > now() - interval '15 minutes'"
    )
    records: list[dict[str, str]] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        records = json.loads(maintenance_sql(statement, "windops_audit") or "null") or []
        actions = {r["action"] for r in records}
        if {"approval.approved", "approval.refused", "authorization.denied"} <= actions:
            break
        time.sleep(3)
    by_agent = [r for r in records if r["actor"] == AGENT]
    check(
        any(r["action"] == "approval.approved" and r["actor"] == MANAGER for r in records)
        and any(r["action"] == "approval.refused" for r in records)
        and any(r["behalf"] == MANAGER for r in by_agent),
        "The approval, the refusals and the agents' denials reached the audit trail, each agent "
        "on its principal's behalf (SEC-010)",
    )  # fmt: skip


def clean_up(case_ids: list[str]) -> None:
    mine = ", ".join(f"'{c}'" for c in case_ids if c.startswith("case_") and c.isascii())
    if not mine:
        return
    sql(
        # The check's own cases only.
        "ALTER TABLE approvals DISABLE TRIGGER approval_decisions_never_change; "  # noqa: S608
        f"DELETE FROM approvals WHERE case_id IN ({mine}); "
        "ALTER TABLE approvals ENABLE TRIGGER approval_decisions_never_change; SELECT 1"
    )
    clean_up_work_orders(case_ids)


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, MANAGER, "maintenance-manager")
        bootstrap(OTHER, MANAGER, "maintenance-manager")
        with ExitStack() as outer:
            cases = Cases(key)
            outer.callback(cases.close)
            outer.callback(lambda: clean_up(cases.opened))
            first, second = (cases.open(ENGINEER, title=f"Approval check {RUN} {n}")
                             for n in (1, 2))  # fmt: skip
            for case_id in (first, second):
                cases.evidence(case_id)
            # The port-forwards to the Maintenance Service end with its pod, so each part opens
            # its own.
            with ExitStack() as stack:
                maintenance_port = stack.enter_context(
                    port_forward("deployment/windops-maintenance", "windops-application", 50051)
                )
                channel = stack.enter_context(
                    blocking_channel(maintenance_port, MAINTENANCE_HOST, bundle)
                )
                maintenance = Caller(maintenance_pb2_grpc.MaintenanceServiceStub(channel), key)
                orders = [submit(maintenance, c, n) for n, c in enumerate((first, second))]
            port = outer.enter_context(
                port_forward("deployment/windops-approval", "windops-application", 50051)
            )
            raw = approval_pb2_grpc.ApprovalServiceStub(
                outer.enter_context(blocking_channel(port, HOST, bundle))
            )
            approvals = Caller(raw, key)
            [approval] = requested(approvals, orders[0].work_order_id)
            [other] = requested(approvals, orders[1].work_order_id)
            check(
                approval.status == "pending" and approval.proposal_version == orders[0].version
                and approval.evidence_snapshot_id == orders[0].evidence_snapshot_id
                and approval.submitted_by == PLANNER,
                f"workorder.submitted opened one pending request, {approval.approval_id}",
            )  # fmt: skip
            refused_to_agents(approvals, raw, key, approval)
            fails_closed(approvals, approval)
            with port_forward(
                "deployment/windops-maintenance", "windops-application", 50051
            ) as maintenance_port:
                approves(approvals, maintenance_port, approval, key)
            superseded(approvals, cases, other, second)
            published_and_audited(approval.approval_id)
    except (
        RuntimeError,
        OSError,
        KeyError,
        ValueError,
        grpc.RpcError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Approval check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
