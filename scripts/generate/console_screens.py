#!/usr/bin/env python3
"""Capture the README's console screenshots, then compose them into one picture.

    make readme-screens

It signs in, in a real browser, through the gateway, against the live demo fleet, as each role
whose screens the picture shows: a Maintenance Planner, a Reliability Engineer, a Maintenance
Manager, an Auditor and a Tenant Administrator, each made for the picture and deleted afterwards.
The healthy demo fleet raises no alarms and holds no parts or work orders, so for the picture it
adds, and removes afterwards:

- three alarms, sent through three turbines' device route, as their controllers would send them;
- a parts catalogue that fits the demo turbines, with stock in each wind farm's store;
- an active case on dogger-east-t01, with its evidence frozen;
- a draft work order from the Maintenance Planner agent, made through the agent's own WindOps MCP
  tool with its own token, for the tenant's fault-investigator. Its plan is written here: no
  model wrote it;
- a planner's own draft, and a planner's submitted work order, waiting in the approval queue;
- an investigation the engineer starts from the screen. It is rehearsed: the real agent team on a
  scripted model, so nothing reaches Claude. Its case is cancelled afterwards.

The knowledge library's search makes one OpenAI embedding call; nothing else is paid for.

The screenshots go to docs/assets/screens, and scripts/generate/console_tour.py composes them.
Run `make services` and the seeds first, with the cluster running.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from google.protobuf.timestamp_pb2 import Timestamp
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.identity.v1 import identity_pb2
from windops.inventory.v1 import inventory_pb2 as inv
from windops.inventory.v1 import inventory_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2 as mnt
from windops.maintenance.v1 import maintenance_pb2_grpc

from scripts.checks.agents.runtime import investigation_bundle
from scripts.checks.agents.tools import Cases, answer
from scripts.checks.consoles.frontend import bootstrap, gateway_spki
from scripts.checks.consoles.planner_setup import PlannerSetup
from scripts.checks.fleet.alarm import sql as alarm_sql
from scripts.checks.fleet.approval import clean_up as clean_up_approvals
from scripts.checks.fleet.asset import HOST as ASSET_HOST
from scripts.checks.fleet.asset import Caller, signing_key
from scripts.checks.fleet.inventory import HOST as INVENTORY_HOST
from scripts.checks.fleet.inventory import sql as inventory_sql
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.fleet.mcp_servers import Server, tls_files
from scripts.checks.fleet.telemetry import device_token, send_to
from scripts.checks.identity.agent_identity import as_service, bundle_of, identity_as
from scripts.checks.identity.identity import Realm, delete_user
from scripts.checks.identity.mfa import Browser, enroll
from scripts.generate.console_tour import SCREENS, build
from scripts.lib.tls_client import authority_file, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import (
    ADMIN_REQUIRED_ACTIONS,
    ADMIN_SECRET_FILE,
    PUBLIC_URL,
    TENANTS,
    admin_client,
    ensure_member,
    ensure_tenant,
    ensure_user,
    keycloak_admin,
    keycloak_public,
    strong_password,
)
from scripts.setup.provision_postgres import read_passwords

REPOSITORY = Path(__file__).resolve().parents[2]
FRONTEND = REPOSITORY / "frontend"
TENANT, TURBINE = "north-sea", "dogger-east-t01"
# The people each screen is captured as: (environment name, email, role). The seed's own users
# (admin@ and operator@) are left alone.
PEOPLE = (
    ("PLANNER", "maintenance.planner@north-sea.example", "maintenance-planner"),
    ("ENGINEER", "reliability.engineer@north-sea.example", "reliability-engineer"),
    ("MANAGER", "maintenance.manager@north-sea.example", "maintenance-manager"),
    ("AUDITOR", "auditor@north-sea.example", "auditor"),
    ("ADMIN", "tenant.administrator@north-sea.example", "tenant-administrator"),
)
INVESTIGATION = "Gearbox oil running hot on DE-T01"
OPENER = "readme-screens-operator"
SOURCE = "turbine-controller-readme"
PROCEDURE = "kn:WS-PRC-GBX-012@3"
# (turbine, code, tag, minutes ago)
ALARMS = (
    ("dogger-east-t01", "GBX_OIL_TEMP_HIGH", "gearbox_oil_temp_c", 6),
    ("dogger-east-t04", "GEN_BEARING_TEMP_HIGH", "generator_bearing_temp_c", 19),
    ("hornsea-west-t03", "NACELLE_VIBRATION_HIGH", "nacelle_vibration_rms", 41),
)
# (part number, name, component, fits any model, stock by wind farm)
PARTS = (
    ("GBX-OCF-CAP-25UF", "Oil cooler fan run capacitor, 25 µF", "gearbox", False,
     {"dogger-east": 4, "hornsea-west": 2}),
    ("GBX-OCF-MTR-4KW", "Oil cooler fan motor, 4 kW", "gearbox", False, {"dogger-east": 1}),
    ("GBX-OIL-FLT-10", "Gearbox oil filter element, 10 µm", "gearbox", False,
     {"dogger-east": 6, "hornsea-west": 3}),
    ("GBX-OIL-VG320-20L", "Gearbox oil, ISO VG 320, 20 L drum", "gearbox", False,
     {"dogger-east": 2}),
    ("GBX-BPV-THERM", "Thermostatic oil bypass valve", "gearbox", False, {"hornsea-west": 1}),
    ("GEN-BRG-6330-C3", "Generator bearing, 6330 C3", "generator", False, {"dogger-east": 2}),
    ("MB-GREASE-EP2-18", "Main bearing grease, EP2, 18 kg", "main_bearing", True,
     {"dogger-east": 3, "hornsea-west": 3}),
)  # fmt: skip


def at(days: float, hour: int) -> datetime:
    return (datetime.now(UTC) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def stamp(value: datetime) -> Timestamp:
    made = Timestamp()
    made.FromDatetime(value)
    return made


def raise_alarms() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    for turbine, code, tag, minutes in ALARMS:
        when = now - timedelta(minutes=minutes)
        report = {
            "schema_version": "1", "tenant_id": TENANT, "turbine_id": turbine,
            "report_id": f"{code}:{tag}:raise:readme:{int(when.timestamp())}", "code": code,
            "tag": tag, "action": "raise", "event_time": when.isoformat(),
            "message": "Above its limit", "source": SOURCE,
        }  # fmt: skip
        answer_ = send_to("alarms", device_token(TENANT, turbine), [report])
        if answer_.status_code != 200:
            raise RuntimeError(f"{turbine}'s alarm was not accepted: {answer_.text}")


def remove_alarms() -> None:
    alarm_sql(
        "DELETE FROM alarm_transitions WHERE alarm_id IN (SELECT alarm_id FROM alarms "  # noqa: S608
        f"WHERE source = '{SOURCE}'); DELETE FROM alarm_reports WHERE report_id LIKE "
        f"'%:readme:%'; DELETE FROM alarms WHERE source = '{SOURCE}'; SELECT 1"
    )


def remove_parts() -> None:
    mine = ", ".join(f"'{number}'" for number, *_ in PARTS)
    inventory_sql(
        "ALTER TABLE stock_movements DISABLE TRIGGER stock_movements_never_change; "  # noqa: S608
        f"DELETE FROM stock_movements WHERE part_number IN ({mine}); "
        f"DELETE FROM reservations WHERE part_number IN ({mine}); "
        f"DELETE FROM stock WHERE part_number IN ({mine}); "
        f"DELETE FROM part_models WHERE part_number IN ({mine}); "
        f"DELETE FROM parts WHERE part_number IN ({mine}); "
        "ALTER TABLE stock_movements ENABLE TRIGGER stock_movements_never_change; SELECT 1"
    )


class Seed:
    def __init__(self, stack: ExitStack, key: str, planner: str) -> None:
        self.stack, self.key, self.planner = stack, key, planner
        self.bundle = client_bundle()

    def stub(self, deployment: str, host: str, make: Any) -> Caller:
        port = self.stack.enter_context(
            port_forward(f"deployment/{deployment}", "windops-application", 50051)
        )
        channel = self.stack.enter_context(blocking_channel(port, host, self.bundle))
        return Caller(make(channel), self.key)

    def parts(self) -> None:
        asset = self.stub("windops-asset", ASSET_HOST, asset_pb2_grpc.AssetServiceStub)
        model = asset.call(
            "GetTurbine", asset_pb2.GetTurbineRequest(turbine_id=TURBINE), self.planner, TENANT
        ).turbine.model
        fits = inv.ModelRef(manufacturer=model.manufacturer, model=model.model)
        inventory = self.stub("windops-inventory", INVENTORY_HOST,
                              inventory_pb2_grpc.InventoryServiceStub)  # fmt: skip
        for number, name, kind, any_model, stock in PARTS:
            part = inv.Part(part_number=number, name=name, component_kind=kind, unit="each",
                            fits=[] if any_model else [fits], fits_any_model=any_model)  # fmt: skip
            inventory.call("PutPart", inv.PutPartRequest(part=part), self.planner, TENANT)
            for farm, count in stock.items():
                inventory.call("AdjustStock", inv.AdjustStockRequest(
                    part_number=number, wind_farm_id=farm, delta=count, reason="Delivered",
                ), self.planner, TENANT)  # fmt: skip

    def agent_draft(self, case_id: str, files: Any) -> None:
        """Through the Maintenance Planner agent's own tool and token, as step 12 drafts."""
        with identity_as(investigation_bundle()) as identity:
            delegation = identity.StartDelegation(
                identity_pb2.StartDelegationRequest(case_id=case_id,
                                                    idempotency_key=f"readme-{case_id}"),
                **as_service("service:investigation", self.key, tenant=TENANT),
            ).delegation.delegation_id  # fmt: skip
        with identity_as(bundle_of("windops-agent-runtime-tls", "windops-application")) as idp:
            token = idp.IssueAgentToken(
                identity_pb2.IssueAgentTokenRequest(
                    delegation_id=delegation, agent="maintenance-planner",
                    audience="windops-mcp-windops", scopes=["mcp.windops.create_draft_work_order"],
                ),
                **as_service("service:agent-runtime", self.key, tenant=TENANT),
            ).access_token  # fmt: skip
        mcp = self.stack.enter_context(
            port_forward("deployment/windops-mcp-windops", "windops-mcp", 8443)
        )
        start = at(3, 7)
        answer(Server("windops", mcp, files), token, "create_draft_work_order", {
            "case_id": case_id, "title": "Inspect the gearbox oil cooling on DE-T01",
            "action": "inspection",
            "summary": "Gearbox oil has run 8 to 10 °C above its trend for six hours at normal "
                       "load, which points to the oil cooler. Inspect the cooler fan and its run "
                       "capacitor, the bypass valve and the oil level, and take an oil sample.",
            "tasks": [
                {"description": "Check the cooler fan, its run capacitor and wiring",
                 "duration_hours": 3, "crew_size": 2, "requires_climb": True},
                {"description": "Check the thermostatic bypass valve and the oil level",
                 "duration_hours": 2, "crew_size": 2, "requires_climb": True},
                {"description": "Take an oil sample for analysis", "duration_hours": 0.5,
                 "crew_size": 1, "requires_climb": True},
            ],
            "parts": [{"part_number": "GBX-OCF-CAP-25UF", "quantity": 1},
                      {"part_number": "GBX-OIL-FLT-10", "quantity": 1}],
            "procedures": [f"{PROCEDURE}#3", f"{PROCEDURE}#4"],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=6)).isoformat(),
            "estimated_downtime_hours": 6,
        })  # fmt: skip

    def planner_work(self, case_id: str) -> str:
        """A planner's own draft, and a submitted work order. Return the submitted one's ID."""
        maintenance = self.stub("windops-maintenance", MAINTENANCE_HOST,
                                maintenance_pb2_grpc.MaintenanceServiceStub)  # fmt: skip
        change = at(6, 7)
        maintenance.call("CreateDraft", mnt.CreateDraftRequest(case_id=case_id, plan=mnt.Plan(
            title="Change the gearbox oil and filter on DE-T01", action="replacement",
            summary="If the oil sample shows degradation, drain and refill the gearbox with "
                    "ISO VG 320 and fit a new filter element.",
            tasks=[mnt.Task(description="Drain and refill the gearbox oil", duration_hours=6,
                            crew_size=2, requires_climb=True),
                   mnt.Task(description="Fit a new filter element and check for leaks",
                            duration_hours=1, crew_size=2, requires_climb=True)],
            parts=[mnt.PartLine(part_number="GBX-OIL-VG320-20L", quantity=3),
                   mnt.PartLine(part_number="GBX-OIL-FLT-10", quantity=1)],
            procedures=[f"{PROCEDURE}#6"], planned_start=stamp(change),
            planned_end=stamp(change + timedelta(hours=8)), estimated_downtime_hours=8,
        )), self.planner, TENANT)  # fmt: skip
        watch = at(1, 6)
        plan = mnt.Plan(
            title="Watch the gearbox oil temperature on DE-T01", action="monitoring",
            summary="Watch the oil temperature against the cooler fan's duty until the inspection.",
            tasks=[mnt.Task(description="Review the oil temperature trend twice a day",
                            duration_hours=1, crew_size=1)],
            planned_start=stamp(watch), planned_end=stamp(watch + timedelta(days=3)),
        )  # fmt: skip
        watched = maintenance.call(
            "CreateDraft", mnt.CreateDraftRequest(case_id=case_id, plan=plan), self.planner, TENANT
        ).work_order
        maintenance.call("SubmitWorkOrder", mnt.SubmitWorkOrderRequest(
            work_order_id=watched.work_order_id, expected_revision=watched.revision,
        ), self.planner, TENANT)  # fmt: skip
        return str(watched.work_order_id)


def capture(password: str, work_order: str) -> None:
    out = REPOSITORY / "docs" / "assets" / "screens"
    env = {
        **{k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"},
        "BASE_URL": PUBLIC_URL.removesuffix("/auth"), "GATEWAY_SPKI": gateway_spki(),
        "NODE_EXTRA_CA_CERTS": authority_file(), "TENANT": TENANT,
        "SCREEN_PASSWORD": password, "SCREEN_TURBINE": TURBINE, "SCREENS_DIR": str(out),
        "SCREEN_INVESTIGATION": INVESTIGATION, "SCREEN_WORK_ORDER": work_order,
        **{f"SCREEN_{name}_EMAIL": email for name, email, _ in PEOPLE},
    }  # fmt: skip
    subprocess.run(["npx", "playwright", "test", "-c", "playwright.screens.config.ts"],
                   cwd=FRONTEND, env=env, check=True)  # fmt: skip


@contextmanager
def people(password: str) -> Iterator[dict[str, str]]:
    """Each role's person, by environment name, with their user ID. The administrator sets up
    their authenticator straight with Keycloak, as the frontend check does, and then signs in
    with a password; nothing the picture shows asks for a second factor."""
    admin = read_passwords(ADMIN_SECRET_FILE)["password"]
    made: dict[str, str] = {}
    with keycloak_admin() as base:
        try:
            with admin_client(base, admin) as api:
                organization = ensure_tenant(api, TENANT, TENANTS[TENANT])
                for name, email, role in PEOPLE:
                    actions = ADMIN_REQUIRED_ACTIONS if name == "ADMIN" else None
                    made[name] = ensure_user(api, email, password, new_password=True,
                                             required_actions=actions)  # fmt: skip
                    ensure_member(api, organization, made[name])
                    bootstrap(TENANT, made[name], role)
            with keycloak_public(through_gateway=False) as public:
                administrator = next(e for n, e, _ in PEOPLE if n == "ADMIN")
                enroll(Browser(Realm(public)), administrator, password)
            yield made
        finally:
            with admin_client(base, admin) as api:
                for _, email, _ in PEOPLE:
                    delete_user(api, email)


def cancel_investigation() -> None:
    """Cancel the case the engineer opened, and remove what its rehearsed run drafted."""
    clean_up_approvals(PlannerSetup("readme", ()).cancel_titled({INVESTIGATION}))


def main() -> int:
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND, check=True)
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=FRONTEND, check=True)
    password = strong_password()
    try:
        with people(password) as made, ExitStack() as stack, keycloak_public():
            bootstrap(TENANT, OPENER, "control-room-operator")
            key = signing_key()
            stack.callback(remove_parts)
            stack.callback(remove_alarms)
            seed = Seed(stack, key, made["PLANNER"])
            files = stack.enter_context(tls_files(seed.bundle))
            cases = Cases(key)
            stack.callback(cases.close)
            stack.callback(lambda: clean_up_approvals(cases.opened))
            stack.callback(cancel_investigation)
            raise_alarms()
            seed.parts()
            case_id = cases.open(OPENER, title="Gearbox oil temperature rising on DE-T01")
            cases.evidence(case_id)
            seed.agent_draft(case_id, files)
            work_order = seed.planner_work(case_id)
            capture(password, work_order)
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    build()
    print(f"Captured {len(SCREENS)} screens, and composed docs/assets/console-tour.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
