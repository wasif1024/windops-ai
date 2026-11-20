"""Work orders through a real gRPC server and SQL database, with stand-in Authorization,
Investigation, Asset, Inventory and Policy services. SQLite has no row-level security, so
`make maintenance-check` proves that on PostgreSQL."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.asset.v1 import asset_pb2
from windops.inventory.v1 import inventory_pb2
from windops.investigation.v1 import investigation_pb2
from windops.maintenance.v1 import maintenance_pb2 as pb
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops.policy.v1 import policy_pb2
from windops_events import parse_event
from windops_grpc import (
    NotFoundError,
    ServiceRuntime,
    Settings,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_maintenance import plans
from windops_maintenance.service import MaintenanceService
from windops_maintenance.store import METADATA, SERVICE_NAME
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(environment="test", version="test", identity_secret=SECRET, grpc_port=0,
                    http_port=0, shutdown_grace_seconds=1)  # fmt: skip
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
CASE = "case_" + "a" * 20
TURBINE = "dogger-east-t01"
PLANNER, OPERATOR = "u-planner", "u-operator"
AGENT = "agent:maintenance-planner"


def stamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value)
    return result


def good_plan(**changes: Any) -> pb.Plan:
    plan = pb.Plan(
        title="Gearbox oil cooler inspection", action="inspection",
        summary="Inspect the oil cooler fan and its capacitor; check the bypass valve.",
        tasks=[pb.Task(description="Inspect the cooler fan", duration_hours=3, crew_size=2,
                       requires_climb=True)],
        procedures=["kn:GBX-OIL-01@3#12"], planned_start=stamp(NOW + timedelta(days=2)),
        planned_end=stamp(NOW + timedelta(days=2, hours=6)), estimated_downtime_hours=6,
        impact_result_id="imp_" + "b" * 20,
    )  # fmt: skip
    for name, value in changes.items():
        if isinstance(value, list):
            plan.ClearField(name)  # type: ignore[arg-type]
            getattr(plan, name).extend(value)
        elif isinstance(value, Timestamp):
            getattr(plan, name).CopyFrom(value)
        else:
            setattr(plan, name, value)
    return plan


@dataclass
class FakeAuthorizer:
    """Grants (who, permission). An agent never gets more than read and draft, as Authorization
    holds it to (SEC-009): unless `generous`, which shows this service refuses it by itself."""

    grants: set[tuple[str, str]]
    generous: bool = False

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        identity = current_identity()
        who = identity.user_id or identity.subject
        if identity.actor and not self.generous and permission not in (
            "investigation.read", "maintenance.draft",
        ):  # fmt: skip
            return False
        return (who, permission) in self.grants


@dataclass
class FakeSources:
    policy: str = "pass"
    policy_down: bool = False
    case_status: str = "investigating"
    proposals: list[Any] = field(default_factory=list)
    # What the Inventory Service says of each part, by number; None when it cannot be asked.
    stock: dict[str, tuple[str, int]] | None = field(default_factory=dict)
    asked: list[Any] = field(default_factory=list)

    async def case(self, case_id: str) -> Any:
        if current_identity().tenant_id != TENANT or case_id != CASE:
            raise NotFoundError("No such case")
        return investigation_pb2.Case(case_id=CASE, turbine_id=TURBINE, status=self.case_status,
                                      evidence_snapshot_id="evs_" + "c" * 20)  # fmt: skip

    async def turbine(self, turbine_id: str) -> Any:
        return asset_pb2.Turbine(turbine_id=turbine_id, wind_farm_id="dogger-east",
                                 status="operating",
                                 model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic",
                                                              model="WS-8.0-167"))  # fmt: skip

    async def parts(self, turbine: Any, lines: list[Any]) -> list[Any]:
        if self.stock is None:
            raise UnavailableError("The Inventory Service cannot be reached right now")
        self.asked.append((turbine.model.model, turbine.wind_farm_id))
        return [inventory_pb2.PartCheck(part_number=line.part_number, quantity=line.quantity,
                                        status=self.stock.get(line.part_number, ("unknown", 0))[0],
                                        available=self.stock.get(line.part_number, ("", 0))[1])
                for line in lines]  # fmt: skip

    async def evaluate(self, case_id: str, proposal: Any, impact_result_id: str, key: str) -> Any:
        if self.policy_down:
            raise UnavailableError("The Policy Service cannot be reached right now")
        self.proposals.append(proposal)
        return policy_pb2.PolicyResult(result_id=f"pol_{len(self.proposals):020d}",
                                       outcome=self.policy)  # fmt: skip


def as_caller(who: str, tenant: str = TENANT, actor: str | None = None,
              person: bool = True) -> Any:  # fmt: skip
    identity = VerifiedIdentity(subject=who, tenant_id=tenant, user_id=who if person else None,
                                auth_time=int(time.time()), actor=actor)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    sources: FakeSources

    async def draft(self, plan: pb.Plan | None = None, who: Any = None, key: str = "") -> Any:
        answer = await self.stub.CreateDraft(
            pb.CreateDraftRequest(case_id=CASE, plan=plan or good_plan(), idempotency_key=key),
            metadata=who or as_caller(PLANNER), timeout=5,
        )  # fmt: skip
        return answer.work_order

    async def submit(self, order: Any, who: Any = None) -> Any:
        return await self.stub.SubmitWorkOrder(
            pb.SubmitWorkOrderRequest(work_order_id=order.work_order_id,
                                      expected_revision=order.revision),
            metadata=who or as_caller(PLANNER), timeout=5,
        )  # fmt: skip

    async def events(self, subject: str) -> list[Any]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        return [parse_event(r.subject, r.envelope).payload for r in rows if r.subject == subject]


GRANTS = {
    (PLANNER, "investigation.read"), (PLANNER, "maintenance.draft"),
    (PLANNER, "maintenance.submit"), (OPERATOR, "investigation.read"),
    ("service:fault-investigator", "investigation.read"),
    ("service:fault-investigator", "maintenance.draft"),
}  # fmt: skip


def with_service(scenario: Callable[[World], Awaitable[None]], *, generous: bool = False,
                 sources: FakeSources | None = None) -> None:  # fmt: skip
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    fakes = sources or FakeSources()

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)

        def register(server: grpc.aio.Server) -> None:
            maintenance_pb2_grpc.add_MaintenanceServiceServicer_to_server(
                MaintenanceService(engine, FakeAuthorizer(GRANTS, generous), fakes,
                                   clock=lambda: NOW),
                server,
            )  # fmt: skip

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = maintenance_pb2_grpc.MaintenanceServiceStub(channel)
                await scenario(World(stub, engine, fakes))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_planner_drafts_fixes_and_submits_a_work_order() -> None:
    """FR-MNT-002: validated as drafted and again at submission; workorder.submitted announces
    it, and every change is kept and audited."""

    async def scenario(world: World) -> None:
        draft = await world.draft(good_plan(tasks=[]))
        assert (draft.status, draft.version, draft.revision) == ("draft", 1, 1)
        assert not draft.validation.passed
        assert {p.code for p in draft.validation.problems if p.blocking} == {"missing"}
        refused = await code_of(world.submit(draft))
        assert refused == grpc.StatusCode.FAILED_PRECONDITION
        updated = (await world.stub.UpdateDraft(
            pb.UpdateDraftRequest(work_order_id=draft.work_order_id, expected_revision=1,
                                  plan=good_plan()),
            metadata=as_caller(PLANNER), timeout=5,
        )).work_order  # fmt: skip
        assert updated.revision == 2 and updated.validation.passed
        assert updated.validation.policy_outcome == "pass"
        submitted = (await world.submit(updated)).work_order
        assert (submitted.status, submitted.submitted_by) == ("submitted", PLANNER)
        got = await world.stub.GetWorkOrder(
            pb.GetWorkOrderRequest(work_order_id=draft.work_order_id),
            metadata=as_caller(OPERATOR), timeout=5,
        )  # fmt: skip
        assert [h.action for h in got.history] == ["drafted", "updated", "submitted"]
        announced = await world.events("workorder.submitted")
        assert [(e.work_order_id, e.proposal_version) for e in announced] == [
            (draft.work_order_id, "1")
        ]
        audited = await world.events("audit.requested.maintenance")
        assert {a.action for a in audited} == {
            "work_order.drafted", "work_order.updated", "work_order.submitted",
        }  # fmt: skip
        # A submitted work order no longer changes as a draft.
        again = await code_of(world.stub.UpdateDraft(
            pb.UpdateDraftRequest(work_order_id=draft.work_order_id, expected_revision=3,
                                  plan=good_plan()),
            metadata=as_caller(PLANNER), timeout=5,
        ))  # fmt: skip
        assert again == grpc.StatusCode.FAILED_PRECONDITION

    with_service(scenario)


def test_an_agent_drafts_but_never_submits_even_if_permissions_said_it_may() -> None:
    """FR-MNT-003, SEC-009: an agent reaches the draft state only. With an Authorization that
    wrongly allowed it everything, this service still refuses."""

    async def scenario(world: World) -> None:
        agent = as_caller(PLANNER, actor=AGENT)
        draft = await world.draft(who=agent)
        assert (draft.drafted_by, draft.drafted_by_agent) == (PLANNER, AGENT)
        assert await code_of(world.submit(draft, who=agent)) == grpc.StatusCode.PERMISSION_DENIED
        withdraw = await code_of(world.stub.WithdrawDraft(
            pb.WithdrawDraftRequest(work_order_id=draft.work_order_id, reason="no"),
            metadata=agent, timeout=5,
        ))  # fmt: skip
        assert withdraw == grpc.StatusCode.PERMISSION_DENIED
        audited = await world.events("audit.requested.maintenance")
        (drafted,) = [a for a in audited if a.action == "work_order.drafted"]
        assert (drafted.actor_type, drafted.actor_id, drafted.on_behalf_of) == (
            "agent", AGENT, PLANNER,
        )  # fmt: skip
        # The person may submit what the agent drafted.
        assert (await world.submit(draft)).work_order.status == "submitted"

    with_service(scenario, generous=True)


def test_the_fault_investigator_drafts_and_no_service_principal_submits() -> None:
    async def scenario(world: World) -> None:
        principal = as_caller("service:fault-investigator", person=False,
                              actor="agent:maintenance-planner")  # fmt: skip
        draft = await world.draft(who=principal)
        assert draft.drafted_by == "service:fault-investigator"
        alone = as_caller("service:fault-investigator", person=False)
        assert await code_of(world.submit(draft, who=alone)) == grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_permissions_and_tenants() -> None:
    async def scenario(world: World) -> None:
        draft = await world.draft()
        # Reads, but may not draft or submit.
        assert await code_of(world.draft(who=as_caller(OPERATOR))) == (
            grpc.StatusCode.PERMISSION_DENIED
        )
        assert await code_of(world.submit(draft, who=as_caller(OPERATOR))) == (
            grpc.StatusCode.PERMISSION_DENIED
        )
        # May not read at all: not found.
        stranger = await code_of(world.stub.GetWorkOrder(
            pb.GetWorkOrderRequest(work_order_id=draft.work_order_id),
            metadata=as_caller("u-nobody"), timeout=5,
        ))  # fmt: skip
        assert stranger == grpc.StatusCode.NOT_FOUND
        other_tenant = await code_of(world.stub.GetWorkOrder(
            pb.GetWorkOrderRequest(work_order_id=draft.work_order_id),
            metadata=as_caller(PLANNER, tenant=OTHER), timeout=5,
        ))  # fmt: skip
        assert other_tenant == grpc.StatusCode.NOT_FOUND

    with_service(scenario)


def test_the_policy_service_blocks_or_cannot_be_asked() -> None:
    """SRS 7.7: a check that did not run never lets a submission through."""
    blocked = FakeSources(policy="block")

    async def scenario(world: World) -> None:
        draft = await world.draft()
        assert not draft.validation.passed and draft.validation.policy_outcome == "block"
        assert await code_of(world.submit(draft)) == grpc.StatusCode.FAILED_PRECONDITION
        proposal = world.sources.proposals[0]
        assert proposal.requires_climb and proposal.action == "inspection"

    with_service(scenario, sources=blocked)

    async def unreachable(world: World) -> None:
        draft = await world.draft()
        assert "policy_unavailable" in {p.code for p in draft.validation.problems if p.blocking}
        assert await code_of(world.submit(draft)) == grpc.StatusCode.FAILED_PRECONDITION

    with_service(unreachable, sources=FakeSources(policy_down=True))

    async def escalated(world: World) -> None:
        draft = await world.draft()
        assert draft.validation.passed and draft.validation.policy_outcome == "escalate"

    with_service(escalated, sources=FakeSources(policy="escalate"))


def test_retries_conflicts_and_closed_cases() -> None:
    async def scenario(world: World) -> None:
        first = await world.draft(key="same")
        again = await world.draft(key="same")
        assert again.work_order_id == first.work_order_id
        stale = await code_of(world.stub.UpdateDraft(
            pb.UpdateDraftRequest(work_order_id=first.work_order_id, expected_revision=7,
                                  plan=good_plan()),
            metadata=as_caller(PLANNER), timeout=5,
        ))  # fmt: skip
        assert stale == grpc.StatusCode.FAILED_PRECONDITION
        listed = await world.stub.ListWorkOrders(
            pb.ListWorkOrdersRequest(case_id=CASE), metadata=as_caller(PLANNER), timeout=5
        )
        assert [w.work_order_id for w in listed.work_orders] == [first.work_order_id]
        world.sources.case_status = "closed"
        assert await code_of(world.draft()) == grpc.StatusCode.FAILED_PRECONDITION

    with_service(scenario)


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"action": "repair", "procedures": []}, "procedures", "missing"),
        ({"procedures": ["see the manual"]}, "procedures", "invalid"),
        ({"planned_start": stamp(NOW - timedelta(hours=1))}, "planned_start", "in_the_past"),
        ({"estimated_downtime_hours": 1000.0}, "estimated_downtime_hours", "out_of_range"),
        ({"action": "teleport"}, "action", "invalid"),
    ],
)
def test_each_plan_rule_names_what_is_wrong(changes: dict[str, Any], field: str,
                                            code: str) -> None:  # fmt: skip
    found = plans.check(good_plan(**changes), NOW)
    assert any(p.field == field and p.code == code and p.blocking for p in found), found


def test_a_plan_without_an_estimate_is_only_noted() -> None:
    found = plans.check(good_plan(impact_result_id=""), NOW)
    assert [(p.code, p.blocking) for p in found] == [("missing", False)]


def test_each_part_is_checked_against_inventory() -> None:
    """M9.2: a part that does not fit or is not in the catalog blocks; a shortage is noted.
    Naming a part reserves nothing."""
    stocked = FakeSources(stock={"FAN-CAP": ("available", 4), "MOTOR": ("short", 0),
                                 "OTHER": ("incompatible", 2)})  # fmt: skip

    def line(part_number: str, quantity: int) -> pb.PartLine:
        return pb.PartLine(part_number=part_number, quantity=quantity)

    async def scenario(world: World) -> None:
        draft = await world.draft(good_plan(parts=[line("FAN-CAP", 2), line("MOTOR", 1)]))
        assert draft.validation.passed
        assert [(p.part_number, p.status, p.available) for p in draft.validation.parts] == [
            ("FAN-CAP", "available", 4), ("MOTOR", "short", 0),
        ]  # fmt: skip
        assert [(p.field, p.code, p.blocking) for p in draft.validation.problems] == [
            ("parts[1]", "part_short", False)
        ]
        assert world.sources.asked == [("WS-8.0-167", "dogger-east")]
        blocked = await world.draft(good_plan(parts=[line("OTHER", 1), line("GONE", 1)]))
        assert {(p.code, p.blocking) for p in blocked.validation.problems} == {
            ("part_incompatible", True), ("part_unknown", True),
        }  # fmt: skip
        assert await code_of(world.submit(blocked)) == grpc.StatusCode.FAILED_PRECONDITION
        twice = await world.draft(good_plan(parts=[line("FAN-CAP", 1), line("FAN-CAP", 1)]))
        assert [p.code for p in twice.validation.problems] == ["duplicate"]
        # Two drafts and the submission's check again; a repeated part is not asked about.
        assert len(world.sources.asked) == 3

    with_service(scenario, sources=stocked)

    async def unreachable(world: World) -> None:
        draft = await world.draft(good_plan(parts=[line("FAN-CAP", 1)]))
        assert [(p.code, p.blocking) for p in draft.validation.problems] == [
            ("inventory_unavailable", True)
        ]
        assert not draft.validation.parts
        # A plan with no parts does not need the Inventory Service.
        assert (await world.draft()).validation.passed

    with_service(unreachable, sources=FakeSources(stock=None))
