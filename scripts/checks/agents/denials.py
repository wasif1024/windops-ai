#!/usr/bin/env python3
"""The Phase 9 exit gate's own proof: an agent can create a draft, and every attempt to submit or
reserve as an agent is denied (SRS 12, FR-MNT-003, FR-INVT-002, FR-INVT-003, SEC-009).

It runs at no cost. An agent drafts as SRS 5.4 step 12 has it: a fault-triggered investigation,
for the tenant's fault-investigator, rehearsed on the real team, tools and tokens (M9.3). Then
every layer an agent could reach for a submission or a reservation is tried, and each refuses:

1. **Its allowlist:** no agent's allowlist, and no MCP server, offers a tool that submits,
   approves, withdraws, reserves, releases or changes stock.
2. **Its tokens:** the Identity Service mints no token for such a tool, nor for a real tool
   outside the agent's own allowlist.
3. **WindOps MCP:** a token for one tool calls no other, the draft tool included.
4. **Authorization:** an agent acting for a planner who may submit and reserve may not, and nor
   may the fault-investigator's; the planner alone may.
5. **The services themselves:** with an identity that has an agent behind it, or no person, the
   Maintenance Service refuses to submit or withdraw the agent's draft, and the Inventory Service
   to reserve, release, adjust stock or change a part, before asking Authorization.

Then nothing changed: the draft is still a draft, the stock is as the planner left it, and every
reservation is the planner's. Every refusal reached the audit trail as `authorization.denied`,
with the agent as the actor on its principal's behalf (SEC-010), for the Denials page.

Run `make services` first, with WINDOPS_AGENT_STAND_IN=on. What it makes is removed at the end.
"""

import json
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

import grpc
import httpx
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2, authorization_pb2_grpc
from windops.identity.v1 import identity_pb2
from windops.inventory.v1 import inventory_pb2 as inv
from windops.inventory.v1 import inventory_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2 as mnt
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops_authorization.policy import AGENT_PERMISSIONS
from windops_identity.targets import AGENT_TOOLS, MCP_TOOLS

from scripts.checks.agents.planner import investigate
from scripts.checks.agents.planner import prove as drafted_in_step_12
from scripts.checks.agents.runtime import investigation_bundle
from scripts.checks.agents.tools import TURBINE, Cases, refused
from scripts.checks.fleet.asset import HOST as ASSET_HOST
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.inventory import HOST as INVENTORY_HOST
from scripts.checks.fleet.inventory import sql as inventory_sql
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.fleet.maintenance import clean_up as clean_up_work_orders
from scripts.checks.fleet.maintenance import sql as maintenance_sql
from scripts.checks.fleet.mcp_servers import Server, tls_files
from scripts.checks.identity.agent_identity import (
    AUTHORIZATION_HOST,
    as_service,
    as_user,
    bundle_of,
    code_of,
    identity_as,
)
from scripts.checks.identity.token_exchange import TENANT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

RUN = str(int(time.time()))
PLANNER, OPENER = "denial-check-planner", "denial-check-operator"
AGENT, PRINCIPAL = "agent:maintenance-planner", "service:fault-investigator"
PART = f"DENY-{RUN}"
# Words in any tool or permission that would take a work order past draft, or touch stock.
WRITES = ("submit", "approve", "withdraw", "reserve", "release", "adjust", "complete", "put_part",
          "update_stock")  # fmt: skip
FORBIDDEN = ("maintenance.submit", "maintenance.approve", "maintenance.complete",
             "inventory.reserve", "inventory.update")  # fmt: skip


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def allowlists() -> None:
    offered = {f"{server}.{tool}" for server, tools in MCP_TOOLS.items() for tool in tools}
    held = {scope for tools in AGENT_TOOLS.values() for scope in tools}
    writes = sorted(s for s in offered | held if any(w in s for w in WRITES))
    check(not writes, "No MCP server offers, and no agent holds, a tool that submits, approves, "
                      "withdraws, reserves, releases or changes stock")  # fmt: skip
    check(
        not set(FORBIDDEN) & AGENT_PERMISSIONS
        and {p for p in AGENT_PERMISSIONS if p.endswith(".read")} | {"maintenance.draft"}
        == AGENT_PERMISSIONS,
        "Authorization holds every agent to reading and maintenance.draft, whoever it acts for",
    )


def tokens(key: str, delegation: str) -> str:
    """Refused tokens, and a real one for find_parts alone, to misuse at WindOps MCP."""

    def issue(agent: str, scope: str) -> Callable[[], Any]:
        def call() -> Any:
            with identity_as(bundle_of("windops-agent-runtime-tls", "windops-application")) as idp:
                return idp.IssueAgentToken(
                    identity_pb2.IssueAgentTokenRequest(
                        delegation_id=delegation, agent=agent, audience="windops-mcp-windops",
                        scopes=[scope],
                    ),
                    **as_service("service:agent-runtime", key, tenant=TENANT),
                )  # fmt: skip

        return call

    invented = code_of(issue("maintenance-planner", "mcp.windops.submit_work_order"))
    outside = code_of(issue("scada-analyst", "mcp.windops.create_draft_work_order"))
    check(
        invented != grpc.StatusCode.OK and outside != grpc.StatusCode.OK,
        f"Identity mints no token for a submitting tool ({invented.name}), nor for the draft tool "
        f"to an agent whose allowlist lacks it ({outside.name})",
    )
    return str(issue("maintenance-planner", "mcp.windops.find_parts")().access_token)


def at_mcp(server: Server, token: str, case_id: str) -> None:
    draft = {"case_id": case_id, "title": "x", "action": "monitoring", "summary": "x",
             "tasks": [{"description": "x", "duration_hours": 1, "crew_size": 1,
                        "requires_climb": False}],
             "planned_start": "2030-01-01T08:00:00Z", "planned_end": "2030-01-01T09:00:00Z",
             "estimated_downtime_hours": 0}  # fmt: skip
    check(
        refused(server, token, "create_draft_work_order", draft)
        and refused(server, token, "submit_work_order", {"work_order_id": "x"}),
        "WindOps MCP refuses a token for find_parts on the draft tool, and has no submit tool",
    )


def at_authorization(key: str, bundle: Any) -> None:
    with (
        port_forward("deployment/windops-authorization", "windops-system", 50051) as port,
        blocking_channel(port, AUTHORIZATION_HOST, bundle) as channel,
    ):
        stub = authorization_pb2_grpc.AuthorizationServiceStub(channel)

        def allowed(permission: str, who: dict[str, Any]) -> bool:
            request = authorization_pb2.CheckRequest(permission=permission)
            return bool(stub.Check(request, **who).allowed)

        person = as_user(PLANNER, key, tenant=TENANT)
        agents = {
            "the planner's agent": as_user(PLANNER, key, tenant=TENANT, actor=AGENT),
            "the fault-investigator's agent": as_service(PRINCIPAL, key, tenant=TENANT,
                                                         actor=AGENT),
        }  # fmt: skip
        check(
            allowed("maintenance.submit", person) and allowed("inventory.reserve", person),
            "The planner may submit and reserve",
        )
        for label, who in agents.items():
            granted = [p for p in FORBIDDEN if allowed(p, who)]
            check(
                not granted and allowed("maintenance.draft", who),
                f"Authorization lets {label} draft, and refuses it {', '.join(FORBIDDEN)}",
            )


def at_services(key: str, bundle: Any, work_order_id: str, stack: ExitStack) -> None:
    def stub(deployment: str, host: str, make: Any) -> Any:
        port = stack.enter_context(
            port_forward(f"deployment/{deployment}", "windops-application", 50051)
        )
        return make(stack.enter_context(blocking_channel(port, host, bundle)))

    maintenance = stub("windops-maintenance", MAINTENANCE_HOST,
                       maintenance_pb2_grpc.MaintenanceServiceStub)  # fmt: skip
    inventory = stub("windops-inventory", INVENTORY_HOST, inventory_pb2_grpc.InventoryServiceStub)
    asset = stub("windops-asset", ASSET_HOST, asset_pb2_grpc.AssetServiceStub)
    work_order = (
        Caller(maintenance, key)
        .call("GetWorkOrder", mnt.GetWorkOrderRequest(work_order_id=work_order_id), PLANNER, TENANT)
        .work_order
    )
    planner = Caller(inventory, key)
    turbine = (
        Caller(asset, key)
        .call("GetTurbine", asset_pb2.GetTurbineRequest(turbine_id=TURBINE), PLANNER, TENANT)
        .turbine
    )
    farm = turbine.wind_farm_id
    fits = inv.ModelRef(manufacturer=turbine.model.manufacturer, model=turbine.model.model)
    part = inv.Part(part_number=PART, name="Denial check part", component_kind="gearbox",
                    unit="each", fits=[fits])  # fmt: skip
    planner.call("PutPart", inv.PutPartRequest(part=part), PLANNER, TENANT)
    planner.call("AdjustStock", inv.AdjustStockRequest(part_number=PART, wind_farm_id=farm,
                                                       delta=3, reason="Denial check"),
                 PLANNER, TENANT)  # fmt: skip
    held = planner.call(
        "ReserveParts",
        inv.ReservePartsRequest(work_order_id=work_order.work_order_id, wind_farm_id=farm,
                                lines=[inv.PartLine(part_number=PART, quantity=1)]),
        PLANNER, TENANT,
    ).reservations[0]  # fmt: skip
    check(
        held.reserved_by == PLANNER, "The planner, a person, reserves a part for the agent's draft"
    )

    attempts: dict[str, Callable[[dict[str, Any]], Any]] = {
        "submit the draft": lambda w: maintenance.SubmitWorkOrder(mnt.SubmitWorkOrderRequest(
            work_order_id=work_order.work_order_id, expected_revision=work_order.revision), **w),
        "withdraw it": lambda w: maintenance.WithdrawDraft(mnt.WithdrawDraftRequest(
            work_order_id=work_order.work_order_id, reason="An agent's attempt"), **w),
        "reserve parts for it": lambda w: inventory.ReserveParts(inv.ReservePartsRequest(
            work_order_id=work_order.work_order_id, wind_farm_id=farm,
            lines=[inv.PartLine(part_number=PART, quantity=1)]), **w),
        "release the planner's reservation": lambda w: inventory.ReleaseReservation(
            inv.ReleaseReservationRequest(reservation_id=held.reservation_id,
                                          reason="An agent's attempt"), **w),
        "adjust stock": lambda w: inventory.AdjustStock(inv.AdjustStockRequest(
            part_number=PART, wind_farm_id=farm, delta=-1, reason="An agent's attempt"), **w),
        "change the part": lambda w: inventory.PutPart(inv.PutPartRequest(
            part=part, expected_version=1), **w),
    }  # fmt: skip
    identities = {
        "the planner's agent": as_user(PLANNER, key, tenant=TENANT, actor=AGENT),
        "the fault-investigator's agent": as_service(PRINCIPAL, key, tenant=TENANT, actor=AGENT),
        "the fault-investigator alone": as_service(PRINCIPAL, key, tenant=TENANT),
    }
    for label, who in identities.items():
        codes = {what: code_of(lambda a=attempt, w=who: a(w)) for what, attempt in attempts.items()}
        allowed = [
            what for what, code in codes.items() if code != grpc.StatusCode.PERMISSION_DENIED
        ]
        check(not allowed, f"As {label}, every attempt is refused: {', '.join(attempts)}")

    got = Caller(maintenance, key).call(
        "GetWorkOrder", mnt.GetWorkOrderRequest(work_order_id=work_order.work_order_id),
        PLANNER, TENANT,
    )  # fmt: skip
    stock = [s for s in planner.call("GetStock", inv.GetStockRequest(part_number=PART),
                                     PLANNER, TENANT).stock if s.wind_farm_id == farm]  # fmt: skip
    reservations = planner.call(
        "ListReservations",
        inv.ListReservationsRequest(work_order_id=work_order.work_order_id),
        PLANNER, TENANT,
    ).reservations  # fmt: skip
    check(
        got.work_order.status == "draft" and not got.work_order.submitted_by
        and [(s.on_hand, s.reserved) for s in stock] == [(3, 1)]
        and [(r.reserved_by, r.status) for r in reservations] == [(PLANNER, "held")],
        "Nothing changed: the draft is still a draft, the stock is as the planner left it, and "
        "the one reservation is the planner's",
    )  # fmt: skip
    done = inv.ReleaseReservationRequest(reservation_id=held.reservation_id, reason="Check over")
    planner.call("ReleaseReservation", done, PLANNER, TENANT)


def recorded(since: datetime) -> None:
    statement = (
        "SELECT set_config('windops.all_tenants', 'on', false); "  # noqa: S608  # Its own clock.
        "SELECT json_agg(DISTINCT actor_id || '|' || on_behalf_of || '|' || resource_id) "
        "FROM audit_records WHERE action = 'authorization.denied' "
        f"AND recorded_at >= '{since.isoformat()}'"
    )
    wanted = {
        f"{AGENT}|{PLANNER}|maintenance.submit", f"{AGENT}|{PLANNER}|inventory.reserve",
        f"{AGENT}|{PLANNER}|inventory.update", f"{AGENT}|{PLANNER}|maintenance.approve",
        f"{AGENT}|{PRINCIPAL}|maintenance.submit", f"{AGENT}|{PRINCIPAL}|inventory.reserve",
        f"{PRINCIPAL}||maintenance.submit", f"{PRINCIPAL}||inventory.reserve",
    }  # fmt: skip
    found: set[str] = set()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        found = set(json.loads(maintenance_sql(statement, "windops_audit") or "null") or [])
        if wanted <= found:
            break
        time.sleep(3)
    check(
        wanted <= found,
        f"Every refusal reached the audit trail as authorization.denied, the agent as the actor "
        f"on its principal's behalf (SEC-010): {len(found)} distinct",
    )


def clean_up() -> None:
    inventory_sql(
        "ALTER TABLE stock_movements DISABLE TRIGGER stock_movements_never_change; "  # noqa: S608
        f"DELETE FROM stock_movements WHERE part_number = '{PART}'; "
        f"DELETE FROM reservations WHERE part_number = '{PART}'; "
        f"DELETE FROM stock WHERE part_number = '{PART}'; "
        f"DELETE FROM part_models WHERE part_number = '{PART}'; "
        f"DELETE FROM parts WHERE part_number = '{PART}'; "
        "ALTER TABLE stock_movements ENABLE TRIGGER stock_movements_never_change; SELECT 1"
    )


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, OPENER, "control-room-operator")
        started = datetime.now(UTC)
        allowlists()
        with ExitStack() as stack:
            files = stack.enter_context(tls_files(bundle))
            stack.callback(clean_up)
            cases = Cases(key)
            stack.callback(cases.close)
            stack.callback(lambda: clean_up_work_orders(cases.opened))
            case_id = cases.open(OPENER, title=f"Denial check {RUN}")
            snapshot = cases.evidence(case_id)
            run_id = investigate(key, case_id, snapshot, "rehearsal")
            work_order_id = drafted_in_step_12(run_id, case_id, "rehearsal")
            check(True, f"An agent created a draft: {work_order_id}, in step 12 (M9.3)")
            with identity_as(investigation_bundle()) as identity:
                delegation = identity.StartDelegation(
                    identity_pb2.StartDelegationRequest(
                        case_id=case_id, idempotency_key=f"denial-{RUN}"
                    ),
                    **as_service("service:investigation", key, tenant=TENANT),
                ).delegation.delegation_id
            token = tokens(key, delegation)
            mcp = stack.enter_context(
                port_forward("deployment/windops-mcp-windops", "windops-mcp", 8443)
            )
            at_mcp(Server("windops", mcp, files), token, case_id)
            at_authorization(key, bundle)
            at_services(key, bundle, work_order_id, stack)
        recorded(started)
    except (RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Agent denial check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
