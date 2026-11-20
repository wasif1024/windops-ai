#!/usr/bin/env python3
"""Prove the Inventory Service on the cluster: parts, compatibility, stock and reservations, and
agents held to suggestions.

With the real services, on a demo turbine and its wind farm's store:

- a Maintenance Planner adds two parts, one fitting the turbine's model and one not, and stocks
  the first; the parts are checked: available, short, incompatible and unknown (FR-INVT-001);
- the planner reserves one for a work order, and releases it; the store's available stock
  follows, the stock ledger cannot be changed, and each change is audited and announced as
  evidence.changed;
- an operator, who has no inventory.read, is refused, and another tenant cannot find the part;
- an agent finds and checks parts through WindOps MCP with its own token, but no identity with
  an agent behind it, or with no person, may reserve or change stock, whatever it acts for
  (FR-INVT-002, FR-INVT-003, SEC-009);
- a work order's validation names each part's availability, and blocks one that does not fit.

Run `make services` first. What it makes is removed at the end.
"""

import json
import subprocess
import sys
import time
from contextlib import ExitStack
from typing import Any

import grpc
import httpx
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.identity.v1 import identity_pb2
from windops.inventory.v1 import inventory_pb2 as pb
from windops.inventory.v1 import inventory_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2, maintenance_pb2_grpc

from scripts.checks.agents.runtime import investigation_bundle
from scripts.checks.agents.tools import TURBINE, Cases, answer
from scripts.checks.fleet.asset import HOST as ASSET_HOST
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.maintenance import clean_up as clean_up_work_orders
from scripts.checks.fleet.maintenance import plan
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
PLANNER, OPERATOR = "inventory-check-planner", "inventory-check-operator"
AGENT = "agent:maintenance-planner"
HOST = "windops-inventory.windops-application.svc.cluster.local"
MAINTENANCE_HOST = "windops-maintenance.windops-application.svc.cluster.local"
RUN = str(int(time.time()))
FIT, MISFIT, NONE = f"CHK-{RUN}-FAN", f"CHK-{RUN}-OTHER", f"CHK-{RUN}-NONE"
WORK_ORDER = "wo_" + RUN.rjust(20, "0")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def sql(statement: str, database: str = "windops_inventory") -> str:
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


def catalog(inventory: Caller, turbine: Any) -> None:
    fitting = pb.ModelRef(manufacturer=turbine.model.manufacturer, model=turbine.model.model)
    for number, fits in ((FIT, fitting), (MISFIT, pb.ModelRef(manufacturer="Other", model="X-3"))):
        inventory.call(
            "PutPart",
            pb.PutPartRequest(
                part=pb.Part(
                    part_number=number,
                    name=f"Inventory check {RUN} cooler fan capacitor",
                    component_kind="gearbox",
                    unit="each",
                    fits=[fits],
                ),
                idempotency_key=f"inv-{RUN}-{number}",
            ),
            PLANNER,
            TENANT,
        )
    stocked = inventory.call(
        "AdjustStock",
        pb.AdjustStockRequest(
            part_number=FIT,
            wind_farm_id=turbine.wind_farm_id,
            delta=2,
            reason="Inventory check delivery",
            idempotency_key=f"inv-{RUN}-stock",
        ),
        PLANNER,
        TENANT,
    ).stock
    check(stocked.available == 2, f"A planner adds two parts and stocks {FIT}: 2 available")
    request = pb.CheckPartsRequest(
        model=fitting,
        wind_farm_id=turbine.wind_farm_id,
        lines=[
            pb.PartLine(part_number=FIT, quantity=3),
            pb.PartLine(part_number=MISFIT, quantity=1),
            pb.PartLine(part_number=NONE, quantity=1),
        ],
    )
    statuses = [c.status for c in inventory.call("CheckParts", request, PLANNER, TENANT).checks]
    check(
        statuses == ["short", "incompatible", "unknown"],
        f"Checked against the {turbine.model.model} and its store: {statuses}",
    )
    refused = code_of(lambda: inventory.call("ListParts", pb.ListPartsRequest(), OPERATOR, TENANT))
    check(refused == grpc.StatusCode.PERMISSION_DENIED, "An operator may not read inventory")
    hidden = code_of(
        lambda: inventory.call("GetPart", pb.GetPartRequest(part_number=FIT), PLANNER, OTHER)
    )
    check(hidden == grpc.StatusCode.NOT_FOUND, "Another tenant cannot find the part")


def reservations(inventory: Caller, raw: Any, key: str, farm: str) -> str:
    held = inventory.call(
        "ReserveParts",
        pb.ReservePartsRequest(
            work_order_id=WORK_ORDER,
            wind_farm_id=farm,
            lines=[pb.PartLine(part_number=FIT, quantity=1)],
            idempotency_key=f"inv-{RUN}-reserve",
        ),
        PLANNER,
        TENANT,
    ).reservations
    (level,) = [
        s
        for s in inventory.call(
            "GetStock", pb.GetStockRequest(part_number=FIT), PLANNER, TENANT
        ).stock
        if s.wind_farm_id == farm
    ]
    check(
        [r.status for r in held] == ["held"] and (level.reserved, level.available) == (1, 1),
        f"The planner reserves one for {WORK_ORDER}: 1 held, 1 available",
    )
    for who, label in (
        (as_user(PLANNER, key, actor=AGENT), "a planner's agent"),
        (
            as_service("service:fault-investigator", key, actor=AGENT),
            "the fault-investigator's agent",
        ),
        (as_service("service:fault-investigator", key), "the fault-investigator alone"),
    ):
        reserve = code_of(
            lambda w=who: raw.ReserveParts(
                pb.ReservePartsRequest(
                    work_order_id=WORK_ORDER,
                    wind_farm_id=farm,
                    lines=[pb.PartLine(part_number=FIT, quantity=1)],
                ),
                **w,
            )
        )
        adjust = code_of(
            lambda w=who: raw.AdjustStock(
                pb.AdjustStockRequest(
                    part_number=FIT, wind_farm_id=farm, delta=-1, reason="Agent attempt"
                ),
                **w,
            )
        )
        check(
            reserve == adjust == grpc.StatusCode.PERMISSION_DENIED,
            f"As {label}, reserving and changing stock are refused by the Inventory Service",
        )
    return str(held[0].reservation_id)


def agent_reads(key: str, server: Server, case_id: str) -> None:
    with identity_as(investigation_bundle()) as identity:
        delegation = identity.StartDelegation(
            identity_pb2.StartDelegationRequest(
                case_id=case_id, idempotency_key=f"inv-{RUN}-fault"
            ),
            **as_service("service:investigation", key),
        ).delegation.delegation_id
    with identity_as(bundle_of("windops-agent-runtime-tls", "windops-application")) as identity:
        token = identity.IssueAgentToken(
            identity_pb2.IssueAgentTokenRequest(
                delegation_id=delegation,
                agent="maintenance-planner",
                audience="windops-mcp-windops",
                scopes=["mcp.windops.find_parts", "mcp.windops.check_parts"],
            ),
            **as_service("service:agent-runtime", key),
        ).access_token
    found = answer(server, token, "find_parts", {"turbine_id": TURBINE, "query": RUN})
    numbers = {p["part_number"]: p["available"] for p in found["parts"]}
    check(
        numbers == {FIT: 1},
        f"The Maintenance Planner agent finds only the part that fits, 1 available: {numbers}",
    )
    checked = answer(
        server,
        token,
        "check_parts",
        {"turbine_id": TURBINE, "parts": [{"part_number": FIT, "quantity": 1}]},
    )
    check(
        [c["status"] for c in checked["checks"]] == ["available"],
        "and checks it through WindOps MCP, holding nothing",
    )


def validation(maintenance: Caller, case_id: str) -> None:
    def draft(number: str) -> Any:
        made = plan()
        made.parts.append(maintenance_pb2.PartLine(part_number=number, quantity=1))
        return maintenance.call(
            "CreateDraft",
            maintenance_pb2.CreateDraftRequest(case_id=case_id, plan=made),
            PLANNER,
            TENANT,
        ).work_order.validation

    fits = draft(FIT)
    check(
        fits.passed and [(p.part_number, p.status) for p in fits.parts] == [(FIT, "available")],
        "A work order's validation names its part available",
    )
    misfit = draft(MISFIT)
    check(
        not misfit.passed
        and ("part_incompatible", True) in {(p.code, p.blocking) for p in misfit.problems},
        "and blocks a part that does not fit the turbine",
    )


def release(inventory: Caller, reservation_id: str, farm: str) -> None:
    released = inventory.call(
        "ReleaseReservation",
        pb.ReleaseReservationRequest(reservation_id=reservation_id, reason="Inventory check over"),
        PLANNER,
        TENANT,
    ).reservation
    (level,) = [
        s
        for s in inventory.call(
            "GetStock", pb.GetStockRequest(part_number=FIT), PLANNER, TENANT
        ).stock
        if s.wind_farm_id == farm
    ]
    check(
        released.status == "released" and level.available == 2,
        "Released, the store has 2 available again",
    )
    try:
        sql(f"UPDATE stock_movements SET quantity = 0 WHERE part_number = '{FIT}'")  # noqa: S608
        changed = True
    except subprocess.CalledProcessError:
        changed = False
    check(not changed, "The stock ledger cannot be changed, even by the service's own login")
    ledger = sql(
        "SELECT string_agg(kind, ',' ORDER BY position) FROM stock_movements "  # noqa: S608
        f"WHERE part_number = '{FIT}'"
    )
    check(ledger == "adjusted,reserved,released", f"The ledger keeps each change: {ledger}")


def recorded() -> None:
    deadline, published = time.monotonic() + 60, "0"
    while time.monotonic() < deadline:
        published = sql(
            "SELECT count(*) FROM outbox_events WHERE subject = 'evidence.changed' "
            "AND published_at IS NOT NULL AND created_at > now() - interval '15 minutes'"
        )
        if int(published) >= 5:
            break
        time.sleep(3)
    check(int(published) >= 5, f"Each change was announced as evidence.changed ({published})")
    statement = (
        # The check's own constant user, never input.
        "SELECT json_agg(DISTINCT action) FROM audit_records WHERE action LIKE 'inventory.%' "  # noqa: S608
        f"AND actor_id = '{PLANNER}' AND recorded_at > now() - interval '15 minutes'"
    )
    wanted = {
        "inventory.part_added",
        "inventory.stock_adjusted",
        "inventory.parts_reserved",
        "inventory.reservation_released",
    }
    actions: set[str] = set()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        actions = set(json.loads(sql(statement, "windops_audit") or "null") or [])
        if wanted <= actions:
            break
        time.sleep(3)
    check(wanted <= actions, f"and audited as the planner: {sorted(actions)}")


def clean_up() -> None:
    """The check's own parts, by its run's part numbers."""
    mine = f"'{FIT}', '{MISFIT}'"
    sql(
        "ALTER TABLE stock_movements DISABLE TRIGGER stock_movements_never_change; "  # noqa: S608
        f"DELETE FROM stock_movements WHERE part_number IN ({mine}); "
        f"DELETE FROM reservations WHERE part_number IN ({mine}); "
        f"DELETE FROM stock WHERE part_number IN ({mine}); "
        f"DELETE FROM part_models WHERE part_number IN ({mine}); "
        f"DELETE FROM parts WHERE part_number IN ({mine}); "
        "ALTER TABLE stock_movements ENABLE TRIGGER stock_movements_never_change; SELECT 1"
    )


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        bootstrap(OTHER, PLANNER, "maintenance-planner")
        with ExitStack() as stack:
            files = stack.enter_context(tls_files(bundle))

            def channel(deployment: str, host: str) -> Any:
                port = stack.enter_context(
                    port_forward(f"deployment/{deployment}", "windops-application", 50051)
                )
                return stack.enter_context(blocking_channel(port, host, bundle))

            raw = inventory_pb2_grpc.InventoryServiceStub(channel("windops-inventory", HOST))
            inventory = Caller(raw, key)
            maintenance = Caller(
                maintenance_pb2_grpc.MaintenanceServiceStub(
                    channel("windops-maintenance", MAINTENANCE_HOST)
                ),
                key,
            )
            asset = Caller(
                asset_pb2_grpc.AssetServiceStub(channel("windops-asset", ASSET_HOST)), key
            )
            mcp = stack.enter_context(
                port_forward("deployment/windops-mcp-windops", "windops-mcp", 8443)
            )
            stack.callback(clean_up)
            cases = Cases(key)
            stack.callback(cases.close)
            stack.callback(lambda: clean_up_work_orders(cases.opened))
            turbine = asset.call(
                "GetTurbine", asset_pb2.GetTurbineRequest(turbine_id=TURBINE), PLANNER, TENANT
            ).turbine
            catalog(inventory, turbine)
            reservation = reservations(inventory, raw, key, turbine.wind_farm_id)
            case_id = cases.open(OPERATOR, title=f"Inventory check {RUN}")
            cases.evidence(case_id)
            agent_reads(key, Server("windops", mcp, files), case_id)
            validation(maintenance, case_id)
            release(inventory, reservation, turbine.wind_farm_id)
        recorded()
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
    print("Inventory check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
