#!/usr/bin/env python3
"""Prove step 14 on the cluster: a person's decision resumes the case's workflow (M10.4).

A real login of the demo operator asks Web BFF to investigate a turbine. The investigation runs,
rehearsed, so nothing is asked of Claude, and at step 13 the case waits for review. Its planner
agent acts for the operator, who may not draft (ADR 0029), so it drafts nothing. Then, with the
real services:

- a Maintenance Planner drafts and submits a work order on the case, and a Maintenance Manager
  approves it;
- approval.decided reaches the Investigation Service, which asks the Agent Runtime for a decision
  run resuming the investigation's run, with no delegation and no call to Claude;
- the run checks the investigation's pins, restores its checkpoint, and records the references:
  the decision, the work order and version, the resumed run, its policy results and evidence;
- the case is approved, its history naming the approval, and announced on case.updated;
- the live stream shows the decision, the run's five steps, its end and the case's move.

Run `make services` and `make gateway-routes` first. Needs local port 9443 free. What it makes is
removed at the end.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

import grpc
import httpx
from windops.approval.v1 import approval_pb2_grpc
from windops.investigation.v1 import investigation_pb2 as investigation
from windops.maintenance.v1 import maintenance_pb2_grpc

from scripts.checks.agents import progress
from scripts.checks.agents.runtime import sql as runtime_sql
from scripts.checks.agents.tools import ENGINEER, Cases
from scripts.checks.fleet import approval as approval_check
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.identity.identity import Realm
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

TENANT = "north-sea"
PLANNER, MANAGER = "resume-check-planner", "resume-check-manager"
RUN = str(int(time.time()))
DECISION_STEPS = (
    "receive_decision", "check_compatibility", "restore", "record_references", "conclude",
)  # fmt: skip


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


def checked_id(value: str, prefix: str) -> str:
    if not value.startswith(prefix) or not value.isascii() or not value[len(prefix) :].isalnum():
        raise RuntimeError(f"Not an ID: {value!r}")
    return value


class Bench:
    def __init__(self, stack: ExitStack, key: str) -> None:
        bundle = client_bundle()

        def stub(deployment: str, host: str, make: Any) -> Caller:
            port = stack.enter_context(
                port_forward(f"deployment/{deployment}", "windops-application", 50051)
            )
            return Caller(make(stack.enter_context(blocking_channel(port, host, bundle))), key)

        self.maintenance = stub("windops-maintenance", MAINTENANCE_HOST,
                                maintenance_pb2_grpc.MaintenanceServiceStub)  # fmt: skip
        self.approvals = stub("windops-approval", approval_check.HOST,
                              approval_pb2_grpc.ApprovalServiceStub)  # fmt: skip
        self.cases = Cases(key)
        stack.callback(self.cases.close)

    def case(self, case_id: str) -> Any:
        return self.cases._api.call(  # The check's own case, as its own engineer.
            "GetCase", investigation.GetCaseRequest(case_id=case_id), ENGINEER, TENANT
        )


def investigated(operator: progress.Operator, bench: Bench) -> tuple[str, str]:
    """The operator's investigation, through the gateway, to step 13: (case, run)."""
    case_id = progress.start(operator)
    bench.cases.opened.append(case_id)
    got = until("the investigation to reach step 13",
                lambda: (c := bench.case(case_id)).case.status != "open"
                and c.case.status != "investigating" and c, timeout=900)  # fmt: skip
    check(got.case.status == "pending_review" and got.case.investigation_status == "completed",
          f"The rehearsed investigation completed, and the case waits for review: "
          f"{got.case.status}, {got.case.investigation_status}")  # fmt: skip
    run_id = checked_id(got.case.run_id, "run_")
    kept = json.loads(runtime_sql(f"SELECT result_json FROM runs WHERE run_id = '{run_id}'",  # noqa: S608
                                  tenant=TENANT) or "{}")  # fmt: skip
    policy = kept.get("policy_result_id")
    check(bool(policy), f"Its recommendation was checked by policy: {policy}")
    return case_id, run_id


def submit_and_approve(bench: Bench, case_id: str) -> Any:
    order = approval_check.submit(bench.maintenance, case_id, 0, planner=PLANNER)
    work_order_id = order.work_order_id
    check(order.status == "submitted", f"A planner drafted and submitted {work_order_id}")
    [request] = approval_check.requested(bench.approvals, work_order_id)
    decision = approval_check.decide(request, idempotency_key=f"resume-{RUN}-approve")
    decided = bench.approvals.call("DecideApproval", decision, MANAGER, TENANT).approval
    check(decided.status == "approved" and decided.decision.decided_by == MANAGER,
          f"A manager approved it: {decided.approval_id}")  # fmt: skip
    return decided


def resumed(bench: Bench, case_id: str, investigation_run: str, approval: Any) -> None:
    got = until("the case to be approved",
                lambda: (c := bench.case(case_id)).case.status == "approved" and c)  # fmt: skip
    last = got.history[-1]
    check((last.action, last.actor_id) == ("approved", "service:investigation")
          and approval.approval_id in last.note,
          f"approval.decided resumed the case's workflow: it is approved, its history naming "
          f"{approval.approval_id}")  # fmt: skip
    row = runtime_sql(
        "SELECT run_id || '|' || status || '|' || delegation_id || '|' || requested_by || '|' "  # noqa: S608  # Checked IDs.
        "|| (SELECT count(*) FROM run_calls c WHERE c.run_id = r.run_id) || '|' || "
        "(SELECT count(*) FROM run_steps s WHERE s.run_id = r.run_id) FROM runs r "
        f"WHERE case_id = '{case_id}' AND workflow = 'decision'",
        tenant=TENANT,
    )
    run_id, status, delegation, requested_by, calls, steps = row.split("|")
    check(
        (status, delegation, requested_by, calls, steps)
        == ("completed", "", "service:investigation", "0", str(len(DECISION_STEPS))),
        f"Investigation asked for decision run {run_id}: all {steps} steps completed, with no "
        "delegation and no call to Claude",
    )  # fmt: skip
    kept = json.loads(runtime_sql(f"SELECT result_json FROM runs WHERE run_id = '{run_id}'",  # noqa: S608
                                  tenant=TENANT))  # fmt: skip
    check(
        kept["resumed_run_id"] == investigation_run
        and kept["approval_id"] == approval.approval_id and kept["decision"] == "approved"
        and kept["work_order_id"] == approval.work_order_id
        and kept["proposal_version"] == str(approval.proposal_version)
        and kept["drafted_by_investigation"] is False and kept["policy_result_ids"]
        and kept["evidence_references"] and kept["decided_by"] == MANAGER,
        f"It resumed {investigation_run} from its checkpoint and recorded the references: "
        f"{len(kept['policy_result_ids'])} policy results, {len(kept['evidence_references'])} "
        "evidence references, and that a person drafted the work order",
    )  # fmt: skip


def streamed(operator: progress.Operator, case_id: str) -> None:
    seen = []
    for event in progress.events(operator, case_id, 0):
        if event["event"] == "finished":
            break
        seen.append((event["event"], event["data"]["step"] or event["data"]["status"]))
    tail = seen[seen.index(("decided", "approved")) :]
    check(
        tail == [("decided", "approved"), ("started", ""),
                 *[("step", name) for name in DECISION_STEPS],
                 ("run_ended", "completed"), ("case_status", "approved")],
        "The live stream shows the decision, the resume's steps, its end and the case approved",
    )  # fmt: skip


def main() -> int:
    try:
        key = signing_key()
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, MANAGER, "maintenance-manager")
        with keycloak_public() as public, ExitStack() as stack:
            operator = progress.Operator(Realm(public), TENANT)
            bench = Bench(stack, key)
            stack.callback(lambda: approval_check.clean_up(bench.cases.opened))
            case_id, run_id = investigated(operator, bench)
            approval = submit_and_approve(bench, case_id)
            resumed(bench, case_id, run_id, approval)
            streamed(operator, case_id)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Resume check passed")
    return 0


if __name__ == "__main__":
    assert KUBE_CONTEXT  # noqa: S101
    sys.exit(main())
