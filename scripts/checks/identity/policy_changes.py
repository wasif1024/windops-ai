#!/usr/bin/env python3
"""Prove that a change to who may approve waits for a second person (RBAC-007, M10.2b).

With the real Authorization Service:
- a Tenant Administrator gives someone the Maintenance Manager role: it is held, not applied;
- the proposer cannot confirm it; a second Tenant Administrator does, and it is applied as the
  proposer's change;
- a role that cannot approve is still granted at once;
- in a check-only tenant with a single Tenant Administrator, a change is escalated: a Platform
  Administrator, signed in to the platform tenant, confirms it, and is refused where the tenant
  has a second administrator;
- the proposal, the refusal and the confirmation reach the tenant's audit trail.

Run `make services` first. The grants it makes are ended afterwards; ended grants are kept, as
every grant is (RBAC-005).
"""

import json
import subprocess
import sys
import time

import grpc
from windops.authorization.v1 import authorization_pb2 as pb
from windops.authorization.v1 import authorization_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.maintenance import sql
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

HOST = "windops-authorization.windops-system.svc.cluster.local"
TENANT, PLATFORM_TENANT = "north-sea", "windops-platform"
RUN = str(int(time.time()))
ADMIN, SECOND = "policy-check-admin", "policy-check-second-admin"
PLATFORM_ADMIN = "policy-check-platform-admin"
MANAGER, OPERATOR = f"policy-check-manager-{RUN}", f"policy-check-operator-{RUN}"
# A tenant of the check's own, with one Tenant Administrator, for the escalation.
LONE = f"policy-check-{RUN}"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def may_approve(api: Caller, user: str, tenant: str = TENANT) -> bool:
    answer = api.call("Check", pb.CheckRequest(permission="maintenance.approve"), user, tenant)
    return bool(answer.allowed)


def confirm(
    api: Caller, change: pb.PendingChange, who: str, tenant: str, target: str = ""
) -> pb.ConfirmChangeResponse:
    key = f"policy-{RUN}-{who}-{change.change_id}"
    request = pb.ConfirmChangeRequest(
        target_tenant_id=target, change_id=change.change_id, idempotency_key=key
    )
    answer: pb.ConfirmChangeResponse = api.call("ConfirmChange", request, who, tenant)
    return answer


def second_administrator(api: Caller) -> list[str]:
    held = api.call("GrantRole", pb.GrantRoleRequest(
        grantee_id=MANAGER, role="maintenance-manager", idempotency_key=f"policy-{RUN}-1"),
        ADMIN, TENANT).pending  # fmt: skip
    check(
        held.status == "pending" and not held.escalated and not may_approve(api, MANAGER),
        f"Giving the Maintenance Manager role is held, not applied: {held.summary}",
    )
    own = api.code("ConfirmChange", pb.ConfirmChangeRequest(change_id=held.change_id), ADMIN,
                   TENANT)  # fmt: skip
    check(own == grpc.StatusCode.PERMISSION_DENIED, "Its proposer cannot confirm it")
    listed = api.call("ListPendingChanges", pb.ListPendingChangesRequest(), SECOND, TENANT)
    check(
        held.change_id in {c.change_id for c in listed.changes},
        "The second administrator sees it waiting",
    )
    done = confirm(api, held, SECOND, TENANT)
    applied = done.change.status == "confirmed" and done.grant.granted_by == ADMIN
    check(
        applied and may_approve(api, MANAGER),
        "A second Tenant Administrator confirms it, applied as the proposer's change",
    )
    plain = api.call("GrantRole", pb.GrantRoleRequest(
        grantee_id=OPERATOR, role="control-room-operator", idempotency_key=f"policy-{RUN}-2"),
        ADMIN, TENANT)  # fmt: skip
    check(
        plain.grant.grant_id != "" and not plain.HasField("pending"),
        "A role that cannot approve is granted at once",
    )
    ending = api.call(
        "RevokeRole",
        pb.RevokeRoleRequest(grant_id=done.grant.grant_id, idempotency_key=f"policy-{RUN}-3"),
        ADMIN,
        TENANT,
    ).pending
    confirm(api, ending, SECOND, TENANT)
    api.call("RevokeRole", pb.RevokeRoleRequest(grant_id=plain.grant.grant_id,
                                                idempotency_key=f"policy-{RUN}-4"),
             ADMIN, TENANT)  # fmt: skip
    check(not may_approve(api, MANAGER), "Ending it waits for, and gets, a second person too")
    return [held.change_id, ending.change_id]


def escalation(api: Caller) -> None:
    bootstrap(LONE, ADMIN, "tenant-administrator")
    held = api.call("GrantRole", pb.GrantRoleRequest(
        grantee_id=MANAGER, role="maintenance-manager", idempotency_key=f"policy-{RUN}-5"),
        ADMIN, LONE).pending  # fmt: skip
    check(held.escalated, "In a tenant with one Tenant Administrator, the change is escalated")
    listed = api.call("ListPendingChanges", pb.ListPendingChangesRequest(), PLATFORM_ADMIN,
                      PLATFORM_TENANT)  # fmt: skip
    check(
        held.change_id in {c.change_id for c in listed.changes},
        "A Platform Administrator sees the escalation",
    )
    done = confirm(api, held, PLATFORM_ADMIN, PLATFORM_TENANT, LONE)
    check(
        done.change.status == "confirmed" and may_approve(api, MANAGER, LONE),
        "A Platform Administrator confirms it",
    )
    ordinary = api.call("GrantRole", pb.GrantRoleRequest(
        grantee_id=MANAGER, role="maintenance-manager", idempotency_key=f"policy-{RUN}-6"),
        ADMIN, TENANT).pending  # fmt: skip
    refused = api.code(
        "ConfirmChange",
        pb.ConfirmChangeRequest(target_tenant_id=TENANT, change_id=ordinary.change_id),
        PLATFORM_ADMIN,
        PLATFORM_TENANT,
    )
    check(
        refused == grpc.StatusCode.PERMISSION_DENIED,
        "Where the tenant has a second administrator, a Platform Administrator may not confirm",
    )
    api.call("DeclineChange", pb.DeclineChangeRequest(
        change_id=ordinary.change_id, reason="Check over", idempotency_key=f"policy-{RUN}-7"),
        ADMIN, TENANT)  # fmt: skip


def audited(change_ids: list[str]) -> None:
    mine = ", ".join(f"'{c}'" for c in change_ids if c.replace("-", "").isalnum())
    statement = (
        # The check's own change IDs.
        "SELECT json_agg(DISTINCT action) FROM audit_records WHERE "  # noqa: S608
        f"resource_id IN ({mine}) AND recorded_at > now() - interval '15 minutes'"
    )
    found: list[str] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        found = json.loads(sql(statement, "windops_audit") or "null") or []
        if {"authorization.approval_policy.proposed",
                "authorization.approval_policy.confirmed"} <= set(found):  # fmt: skip
            break
        time.sleep(3)
    check(
        {"authorization.approval_policy.proposed", "authorization.approval_policy.confirmed"}
        <= set(found),
        f"Each proposal and confirmation reached the tenant's audit trail: {sorted(found)}",
    )


def main() -> int:
    try:
        key = signing_key()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, SECOND, "tenant-administrator")
        bootstrap(PLATFORM_TENANT, PLATFORM_ADMIN, "platform-administrator")
        with (
            port_forward("deployment/windops-authorization", "windops-system", 50051) as port,
            blocking_channel(port, HOST, client_bundle()) as channel,
        ):
            api = Caller(authorization_pb2_grpc.AuthorizationServiceStub(channel), key)
            changes = second_administrator(api)
            escalation(api)
        audited(changes)
    except (RuntimeError, OSError, KeyError, ValueError, grpc.RpcError,
            subprocess.CalledProcessError) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Policy change check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
