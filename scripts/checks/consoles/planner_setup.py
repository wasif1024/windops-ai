"""What the planner screens' browser journey needs, made with the real services and removed after.

For each screen size: an active case on the demo turbine, and a check-only part that fits the
turbine's model, with 2 in its wind farm's store. They are made over mutual TLS as a seeding
Maintenance Planner, as the inventory check does (M9.4).

For the approval queue (M10.2), each screen size also gets a second case, frozen, with a work
order the seeding planner submits, so a Maintenance Manager has a pending request to approve.
"""

from collections.abc import Iterable
from contextlib import ExitStack

from windops.approval.v1 import approval_pb2_grpc
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.inventory.v1 import inventory_pb2 as inv
from windops.inventory.v1 import inventory_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2_grpc

from scripts.checks.agents.tools import ENGINEER, TURBINE, Cases
from scripts.checks.fleet import approval as approval_check
from scripts.checks.fleet.asset import HOST as ASSET_HOST
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.inventory import HOST as INVENTORY_HOST
from scripts.checks.fleet.inventory import sql as inventory_sql
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

TENANT = "north-sea"
SEEDER, OPENER = "frontend-check-planner-seed", "frontend-check-case-opener"


class PlannerSetup:
    def __init__(self, run: str, projects: Iterable[str]) -> None:
        self.parts = {p: f"UI-{run}-{p.upper()}" for p in projects}
        self.cases = {p: f"Planner screen {p} {run}" for p in projects}
        # The work order each screen size's Maintenance Manager approves.
        self.submitted: dict[str, str] = {}
        self._stack = ExitStack()
        self._opened: list[str] = []

    def make(self) -> None:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, SEEDER, "maintenance-planner")
        bootstrap(TENANT, OPENER, "control-room-operator")

        def stub(deployment: str, host: str, make: object) -> Caller:
            port = self._stack.enter_context(
                port_forward(f"deployment/{deployment}", "windops-application", 50051)
            )
            channel = self._stack.enter_context(blocking_channel(port, host, bundle))
            return Caller(make(channel), key)  # type: ignore[operator]

        asset = stub("windops-asset", ASSET_HOST, asset_pb2_grpc.AssetServiceStub)
        inventory = stub(
            "windops-inventory", INVENTORY_HOST, inventory_pb2_grpc.InventoryServiceStub
        )
        turbine = asset.call(
            "GetTurbine", asset_pb2.GetTurbineRequest(turbine_id=TURBINE), SEEDER, TENANT
        ).turbine
        fits = inv.ModelRef(manufacturer=turbine.model.manufacturer, model=turbine.model.model)
        for number in self.parts.values():
            inventory.call(
                "PutPart",
                inv.PutPartRequest(part=inv.Part(part_number=number, unit="each",
                                                 name="Frontend check cooler fan capacitor",
                                                 component_kind="gearbox", fits=[fits])),
                SEEDER, TENANT,
            )  # fmt: skip
            inventory.call(
                "AdjustStock",
                inv.AdjustStockRequest(part_number=number, wind_farm_id=turbine.wind_farm_id,
                                       delta=2, reason="Frontend check stock"),
                SEEDER, TENANT,
            )  # fmt: skip
        cases = Cases(key)
        self._stack.callback(cases.close)
        for title in self.cases.values():
            cases.open(OPENER, title=title)
        self._opened = cases.opened
        maintenance = stub(
            "windops-maintenance", MAINTENANCE_HOST, maintenance_pb2_grpc.MaintenanceServiceStub
        )
        approvals = stub("windops-approval", approval_check.HOST,
                         approval_pb2_grpc.ApprovalServiceStub)  # fmt: skip
        bootstrap(TENANT, approval_check.PLANNER, "maintenance-planner")
        bootstrap(TENANT, approval_check.MANAGER, "maintenance-manager")
        for n, project in enumerate(self.parts):
            case_id = cases.open(ENGINEER, title=f"Approval screen {project} {self.parts[project]}")
            cases.evidence(case_id)
            order = approval_check.submit(maintenance, case_id, 100 + n)
            approval_check.requested(approvals, order.work_order_id)
            self.submitted[project] = str(order.work_order_id)

    def remove(self) -> None:
        try:
            approval_check.clean_up(self._opened)
            mine = ", ".join(f"'{n}'" for n in self.parts.values())
            # Only this run's own part numbers, which it made itself.
            inventory_sql(
                "ALTER TABLE stock_movements DISABLE TRIGGER stock_movements_never_change; "  # noqa: S608
                f"DELETE FROM stock_movements WHERE part_number IN ({mine}); "
                f"DELETE FROM reservations WHERE part_number IN ({mine}); "
                f"DELETE FROM stock WHERE part_number IN ({mine}); "
                f"DELETE FROM part_models WHERE part_number IN ({mine}); "
                f"DELETE FROM parts WHERE part_number IN ({mine}); "
                "ALTER TABLE stock_movements ENABLE TRIGGER stock_movements_never_change; SELECT 1"
            )
        finally:
            self._stack.close()
