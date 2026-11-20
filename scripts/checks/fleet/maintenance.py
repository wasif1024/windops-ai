#!/usr/bin/env python3
"""Prove the Maintenance Service on the cluster: plans, validation, and agents held to drafts.

A case is opened on a demo turbine and its evidence frozen. Then, with the real services:

- a Maintenance Planner drafts an incomplete plan, sees its problems, fixes it, and submits it;
  the submission is published as workorder.submitted, and every change is audited (FR-MNT-002);
- an operator, who may read but not plan, is refused a draft;
- an agent, the Maintenance Planner agent under the tenant's fault-investigator, drafts a work
  order through WindOps MCP with its own token, audited as the agent on the principal's behalf;
- no agent can submit (FR-MNT-003, SEC-009): no agent's allowlist names a tool that submits,
  approves or reserves, and an identity with an agent behind it is refused by the Maintenance
  Service itself, even for a person who may submit;
- another tenant cannot find the work order.

Run `make services` first. What it makes is removed at the end.
"""

import json
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from google.protobuf.timestamp_pb2 import Timestamp
from windops.identity.v1 import identity_pb2
from windops.maintenance.v1 import maintenance_pb2 as pb
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops_identity.targets import AGENT_TOOLS

from scripts.checks.agents.runtime import investigation_bundle
from scripts.checks.agents.tools import Cases, answer
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.mcp_servers import Server, tls_files
from scripts.checks.identity.agent_identity import (
    as_service,
    as_user,
    bundle_of,
    code_of,
    identity_as,
)
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

TENANT, OTHER = "north-sea", "atlas-wind"
PLANNER, OPERATOR = "maintenance-check-planner", "maintenance-check-operator"
AGENT = "agent:maintenance-planner"
HOST = "windops-maintenance.windops-application.svc.cluster.local"
RUN = str(int(time.time()))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def sql(statement: str, database: str = "windops_maintenance") -> str:
    """As the service's own login, across tenants."""
    return (
        subprocess.run(
            [
                "kubectl",
                "--context",
                KUBE_CONTEXT,
                "--namespace=windops-data",
                "exec",
                "windops-postgres-0",
                "--",
                "psql",
                "--no-psqlrc",
                "--quiet",
                "--tuples-only",
                "--no-align",
                f"--username={database}",
                f"--dbname={database}",
                "--set=ON_ERROR_STOP=1",
                f"--command=SELECT set_config('windops.all_tenants', 'on', false); {statement}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .splitlines()[-1]
    )


def stamp(value: datetime) -> Timestamp:
    made = Timestamp()
    made.FromDatetime(value)
    return made


def plan(**changes: Any) -> pb.Plan:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    made = pb.Plan(
        title=f"Maintenance check {RUN}: gearbox oil cooler",
        action="monitoring",
        summary="Watch the gearbox oil temperature against the cooler fan's duty for a week.",
        tasks=[
            pb.Task(
                description="Review the oil temperature trend remotely",
                duration_hours=2,
                crew_size=1,
                requires_climb=False,
            )
        ],
        planned_start=stamp(start),
        planned_end=stamp(start + timedelta(hours=4)),
        estimated_downtime_hours=0,
    )
    for name in changes:
        made.ClearField(name)  # type: ignore[arg-type]
    return made


def prove(maintenance: Caller, raw: Any, key: str, server: Server, case_id: str) -> str:
    first = maintenance.call(
        "CreateDraft",
        pb.CreateDraftRequest(
            case_id=case_id,
            plan=plan(tasks=[]),
            idempotency_key=f"mnt-{RUN}-1",
        ),
        PLANNER,
        TENANT,
    ).work_order
    problems = {p.code for p in first.validation.problems if p.blocking}
    check(
        first.status == "draft" and not first.validation.passed and "missing" in problems,
        f"A planner's draft without tasks is kept, its problems named: {sorted(problems)}",
    )
    fixed = maintenance.call(
        "UpdateDraft",
        pb.UpdateDraftRequest(
            work_order_id=first.work_order_id,
            expected_revision=first.revision,
            plan=plan(),
        ),
        PLANNER,
        TENANT,
    ).work_order
    check(
        fixed.validation.passed
        and fixed.validation.turbine_status != ""
        and fixed.validation.policy_outcome in ("pass", "escalate"),
        f"Fixed, it passes: turbine {fixed.validation.turbine_status}, Policy Service "
        f"{fixed.validation.policy_outcome} ({fixed.validation.policy_result_id})",
    )
    submitted = maintenance.call(
        "SubmitWorkOrder",
        pb.SubmitWorkOrderRequest(
            work_order_id=fixed.work_order_id,
            expected_revision=fixed.revision,
            idempotency_key=f"mnt-{RUN}-submit",
        ),
        PLANNER,
        TENANT,
    ).work_order
    check(
        submitted.status == "submitted" and submitted.submitted_by == PLANNER,
        "The planner submits it, validated again first",
    )
    refused = code_of(
        lambda: maintenance.call(
            "CreateDraft", pb.CreateDraftRequest(case_id=case_id, plan=plan()), OPERATOR, TENANT
        )
    )
    check(
        refused == grpc.StatusCode.PERMISSION_DENIED,
        "An operator, who may read the case, is refused a draft",
    )
    hidden = code_of(
        lambda: maintenance.call(
            "GetWorkOrder",
            pb.GetWorkOrderRequest(work_order_id=submitted.work_order_id),
            PLANNER,
            OTHER,
        )
    )
    check(hidden == grpc.StatusCode.NOT_FOUND, "Another tenant cannot find it")

    # The agent path: the Maintenance Planner agent, for the tenant's fault-investigator.
    with identity_as(investigation_bundle()) as identity:
        delegation = identity.StartDelegation(
            identity_pb2.StartDelegationRequest(
                case_id=case_id, idempotency_key=f"mnt-{RUN}-fault"
            ),
            **as_service("service:investigation", key),
        ).delegation.delegation_id
    with identity_as(bundle_of("windops-agent-runtime-tls", "windops-application")) as identity:
        token = identity.IssueAgentToken(
            identity_pb2.IssueAgentTokenRequest(
                delegation_id=delegation,
                agent="maintenance-planner",
                audience="windops-mcp-windops",
                scopes=["mcp.windops.create_draft_work_order"],
            ),
            **as_service("service:agent-runtime", key),
        ).access_token
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    drafted = answer(
        server,
        token,
        "create_draft_work_order",
        {
            "case_id": case_id,
            "title": f"Maintenance check {RUN}: agent's draft",
            "action": "monitoring",
            "summary": "Watch the oil temperature for a week.",
            "tasks": [
                {
                    "description": "Review the trend",
                    "duration_hours": 2,
                    "crew_size": 1,
                    "requires_climb": False,
                }
            ],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=4)).isoformat(),
            "estimated_downtime_hours": 0,
        },
    )
    check(
        drafted["status"] == "draft" and drafted["drafted_by_agent"] == AGENT,
        f"The Maintenance Planner agent drafts {drafted['work_order_id']} through WindOps MCP, "
        "with its own token",
    )
    writes = sorted(
        s
        for tools in AGENT_TOOLS.values()
        for s in tools
        if any(w in s for w in ("submit", "approve", "reserve", "complete"))
    )
    check(not writes, "No agent's allowlist names a tool that submits, approves or reserves")
    for who, label in (
        (as_user(PLANNER, key, actor=AGENT), "a planner's agent"),
        (
            as_service("service:fault-investigator", key, actor=AGENT),
            "the fault-investigator's agent",
        ),
    ):
        attempt = code_of(
            lambda w=who: raw.SubmitWorkOrder(
                pb.SubmitWorkOrderRequest(
                    work_order_id=drafted["work_order_id"], expected_revision=drafted["revision"]
                ),
                **w,
            )
        )
        check(
            attempt == grpc.StatusCode.PERMISSION_DENIED,
            f"Submitting as {label} is refused by the Maintenance Service itself",
        )
    got = maintenance.call(
        "GetWorkOrder",
        pb.GetWorkOrderRequest(work_order_id=drafted["work_order_id"]),
        PLANNER,
        TENANT,
    )
    check(
        got.work_order.status == "draft" and [h.agent for h in got.history] == [AGENT],
        "It is still a draft, and its history names the agent",
    )
    return str(submitted.work_order_id)


def audited(work_order_id: str) -> None:
    deadline, found = time.monotonic() + 60, ""
    while time.monotonic() < deadline:
        found = sql(
            "SELECT count(*) FROM outbox_events WHERE subject = 'workorder.submitted' "
            "AND published_at IS NOT NULL"
        )
        if int(found) > 0:
            break
        time.sleep(3)
    check(int(found) > 0, "workorder.submitted was published, for Approval and Notification")
    statement = (
        "SELECT json_agg(json_build_object('action', action, 'actor', actor_id, "
        "'behalf', on_behalf_of)) FROM audit_records WHERE resource_type = 'work_order' "
        "AND recorded_at > now() - interval '15 minutes' AND action LIKE 'work_order.%'"
    )
    records: list[dict[str, str]] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        records = json.loads(sql(statement, "windops_audit") or "null") or []
        if {"work_order.submitted", "work_order.drafted"} <= {r["action"] for r in records} and any(
            r["actor"] == AGENT for r in records
        ):
            break
        time.sleep(3)
    by_agent = [r for r in records if r["actor"] == AGENT]
    check(
        any(r["action"] == "work_order.submitted" and r["actor"] == PLANNER for r in records)
        and bool(by_agent)
        and all(r["behalf"] == "service:fault-investigator" for r in by_agent),
        "Each change reached the audit trail: the submission as the planner, the agent's draft "
        "as the agent on the fault-investigator's behalf (SEC-010)",
    )
    del work_order_id


def clean_up(case_ids: list[str]) -> None:
    """The check's own work orders, by its own cases' IDs."""
    if not case_ids:
        return
    mine = ", ".join(f"'{c}'" for c in case_ids if c.startswith("case_") and c.isascii())
    orders = f"(SELECT work_order_id FROM work_orders WHERE case_id IN ({mine}))"  # noqa: S608
    # Only the check's own case IDs, which it made itself.
    sql(
        "ALTER TABLE work_order_history DISABLE TRIGGER work_order_history_never_changes; "  # noqa: S608
        f"DELETE FROM work_order_history WHERE work_order_id IN {orders}; "
        f"DELETE FROM work_orders WHERE case_id IN ({mine}); "
        "ALTER TABLE work_order_history ENABLE TRIGGER work_order_history_never_changes; SELECT 1"
    )


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        bootstrap(OTHER, PLANNER, "maintenance-planner")
        with ExitStack() as stack:
            files = stack.enter_context(tls_files(bundle))
            port = stack.enter_context(
                port_forward("deployment/windops-maintenance", "windops-application", 50051)
            )
            channel = stack.enter_context(blocking_channel(port, HOST, bundle))
            mcp = stack.enter_context(
                port_forward("deployment/windops-mcp-windops", "windops-mcp", 8443)
            )
            raw = maintenance_pb2_grpc.MaintenanceServiceStub(channel)
            cases = Cases(key)
            stack.callback(cases.close)
            stack.callback(lambda: clean_up(cases.opened))
            case_id = cases.open(OPERATOR, title=f"Maintenance check {RUN}")
            cases.evidence(case_id)
            submitted = prove(Caller(raw, key), raw, key, Server("windops", mcp, files), case_id)
        audited(submitted)
    except (
        RuntimeError,
        OSError,
        KeyError,
        ValueError,
        grpc.RpcError,
        httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Maintenance check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
