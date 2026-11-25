#!/usr/bin/env python3
"""Prove the Notification Service on the cluster (M10.5, FR-NOT-001, FR-NOT-002).

An engineer opens a case on a demo turbine, and its evidence is frozen. A Maintenance Planner
drafts and submits a work order, and a Maintenance Manager approves it. Then, with the real
services:

- the submission reaches the manager, who may approve it, and not the planner who submitted it
  (RBAC-004); the decision reaches the planner; the case's approval reaches the engineer who
  opened it;
- each is in the app at once, delivered, and its email is rendered and recorded, as no email
  provider is configured (ADR 0037);
- what they say is identifiers and a link: never the decision's reason, evidence or an address;
- each person sees only their own, and marks them read;
- an auditor sees every delivery for the case, by user ID only; the planner may not.

Run `make services` first. What it makes is removed at the end.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

import grpc
import httpx
from windops.approval.v1 import approval_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops.notification.v1 import notification_pb2 as pb
from windops.notification.v1 import notification_pb2_grpc

from scripts.checks.agents.tools import Cases
from scripts.checks.fleet import approval as approval_check
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.fleet.maintenance import sql as maintenance_sql
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

TENANT = "north-sea"
ENGINEER, PLANNER = "notification-check-engineer", "notification-check-planner"
MANAGER, AUDITOR = "notification-check-manager", "notification-check-auditor"
HOST = "windops-notification.windops-application.svc.cluster.local"


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


def sql(statement: str) -> str:
    return maintenance_sql(statement, "windops_notification")


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
        self.notifications = stub("windops-notification", HOST,
                                  notification_pb2_grpc.NotificationServiceStub)  # fmt: skip
        self.cases = Cases(key)
        stack.callback(self.cases.close)

    def mine(self, who: str, case_id: str) -> list[Any]:
        answer = self.notifications.call(
            "ListMyNotifications", pb.ListMyNotificationsRequest(page_size=200), who, TENANT
        )
        return [n for n in answer.notifications if n.case_id == case_id]

    def told(self, who: str, case_id: str, kind: str) -> Any:
        """The person's notification of this kind, once its email is recorded too."""

        def found() -> Any:
            for n in self.mine(who, case_id):
                ways = {d.channel: d.status for d in n.deliveries}
                if n.kind == kind and ways.get("email") in ("recorded", "failed"):
                    return n
            return None

        return until(f"{who} to be told {kind}", found)


def delivered(note: Any) -> bool:
    ways = {d.channel: d.status for d in note.deliveries}
    return ways == {"in_app": "delivered", "email": "recorded"}


def prove(bench: Bench) -> None:
    case_id = bench.cases.open(ENGINEER, title="Notification check")
    bench.cases.evidence(case_id)
    order = approval_check.submit(bench.maintenance, case_id, 0, planner=PLANNER)
    asked = bench.told(MANAGER, case_id, "approval_requested")
    check(delivered(asked) and asked.link == "/screens/approvals"
          and order.work_order_id in asked.title,
          f"The submission reached the manager: \"{asked.title}\", in the app and its email "
          "recorded")  # fmt: skip
    check(not [n for n in bench.mine(PLANNER, case_id) if n.kind == "approval_requested"],
          "The planner who submitted it is never asked to approve it (RBAC-004)")  # fmt: skip

    [request] = approval_check.requested(bench.approvals, order.work_order_id)
    reason = f"Notification check {time.time():.0f}: the bearing temperature fits"
    decision = approval_check.decide(request, reason=reason,
                                     idempotency_key=f"ntf-{time.time():.0f}")  # fmt: skip
    bench.approvals.call("DecideApproval", decision, MANAGER, TENANT)
    decided = bench.told(PLANNER, case_id, "decision_made")
    check(delivered(decided) and decided.title.endswith("was approved"),
          f"The decision reached the planner: \"{decided.title}\"")  # fmt: skip
    approved = bench.told(ENGINEER, case_id, "case_approved")
    check(
        delivered(approved) and approved.link == "/screens/investigations",
        f'The case\'s approval reached the engineer who opened it: "{approved.title}"',
    )

    everything = [n for who in (ENGINEER, PLANNER, MANAGER) for n in bench.mine(who, case_id)]
    text = " ".join(f"{n.title} {n.body} {n.link}" for n in everything)
    check("bearing temperature" not in text and "@" not in text and "evs_" not in text,
          f"Its {len(everything)} notifications say identifiers and a link: no reason, "
          "evidence or address (FR-NOT-002)")  # fmt: skip

    others = [n.notification_id for n in bench.mine(MANAGER, case_id)]
    none = bench.notifications.call(
        "MarkNotificationsRead", pb.MarkNotificationsReadRequest(notification_ids=others),
        ENGINEER, TENANT,
    )  # fmt: skip
    marked = bench.notifications.call(
        "MarkNotificationsRead",
        pb.MarkNotificationsReadRequest(notification_ids=[approved.notification_id]),
        ENGINEER, TENANT,
    )  # fmt: skip
    still = [n for n in bench.mine(ENGINEER, case_id) if not n.HasField("read_at")]
    check(none.marked == 0 and marked.marked == 1 and not still,
          "Each person marks only their own read")  # fmt: skip

    refused = bench.notifications.code("ListDeliveries", pb.ListDeliveriesRequest(case_id=case_id),
                                       PLANNER, TENANT)  # fmt: skip
    listed = bench.notifications.call("ListDeliveries", pb.ListDeliveriesRequest(case_id=case_id),
                                      AUDITOR, TENANT).deliveries  # fmt: skip
    people = {d.user_id for d in listed}
    check(
        refused == grpc.StatusCode.PERMISSION_DENIED
        and {ENGINEER, PLANNER, MANAGER} <= people
        and all("@" not in d.user_id for d in listed),
        f"An auditor sees the case's {len(listed)} deliveries by user ID; the planner may not",
    )


def clean_up(case_ids: list[str]) -> None:
    approval_check.clean_up(case_ids)
    ours = [c for c in case_ids if c.startswith("case_") and c.isascii() and c[5:].isalnum()]
    if not ours:
        return
    listed = ", ".join(f"'{c}'" for c in ours)
    heard = " OR ".join(f"facts LIKE '%{c}%'" for c in ours)
    sql(
        # The check's own cases only.
        "DELETE FROM deliveries WHERE notification_id IN (SELECT notification_id FROM "  # noqa: S608
        f"notifications WHERE case_id IN ({listed})); "
        f"DELETE FROM notifications WHERE case_id IN ({listed}); "
        f"DELETE FROM work_orders WHERE case_id IN ({listed}); "
        f"DELETE FROM heard_events WHERE {heard}; SELECT 1"
    )


def main() -> int:
    try:
        key = signing_key()
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, MANAGER, "maintenance-manager")
        bootstrap(TENANT, AUDITOR, "auditor")
        with ExitStack() as stack:
            bench = Bench(stack, key)
            stack.callback(lambda: clean_up(bench.cases.opened))
            prove(bench)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Notification check passed")
    return 0


if __name__ == "__main__":
    assert KUBE_CONTEXT  # noqa: S101
    sys.exit(main())
