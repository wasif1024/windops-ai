"""Parts, stock and reservations through a real gRPC server and SQL database, with a stand-in
Authorization Service. SQLite has no row-level security or triggers, so `make inventory-check`
proves those on PostgreSQL."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.inventory.v1 import inventory_pb2 as pb
from windops.inventory.v1 import inventory_pb2_grpc
from windops_events import parse_event
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, current_identity, sign_identity
from windops_inventory import rules
from windops_inventory.service import InventoryService
from windops_inventory.store import METADATA, SERVICE_NAME, STOCK_MOVEMENTS
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(environment="test", version="test", identity_secret=SECRET, grpc_port=0,
                    http_port=0, shutdown_grace_seconds=1)  # fmt: skip
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
FARM, ELSEWHERE = "dogger-east", "hornsea-west"
MODEL = pb.ModelRef(manufacturer="WindOps Synthetic", model="WS-8.0-167")
PLANNER, ENGINEER, OPERATOR, LOCAL = "u-planner", "u-engineer", "u-operator", "u-local"
AGENT = "agent:maintenance-planner"
WORK_ORDER = "wo_" + "a" * 20
FAN = "GBX-FAN-CAP-25UF"


@dataclass
class FakeAuthorizer:
    """Grants (who, permission, wind farm), "" for the whole tenant. An agent never gets more
    than reading, as Authorization holds it to (SEC-009): unless `generous`, which shows this
    service refuses it by itself."""

    grants: set[tuple[str, str, str]]
    generous: bool = False

    def _holds(self, permission: str, farm: str) -> bool:
        identity = current_identity()
        who = identity.user_id or identity.subject
        if identity.actor and not self.generous and not permission.endswith(".read"):
            return False
        return (who, permission, "") in self.grants or (
            bool(farm) and (who, permission, farm) in self.grants
        )

    async def allowed(self, permission: str, wind_farm_id: str = "") -> bool:
        return self._holds(permission, wind_farm_id)

    async def anywhere(self, permission: str) -> bool:
        return any(
            self._holds(permission, farm) for who, held, farm in self.grants if held == permission
        )


GRANTS = {
    (PLANNER, "inventory.read", ""), (PLANNER, "inventory.update", ""),
    (PLANNER, "inventory.reserve", ""), (ENGINEER, "inventory.read", ""),
    (LOCAL, "inventory.read", ELSEWHERE),
    ("service:fault-investigator", "inventory.read", ""),
}  # fmt: skip


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


def fan(**changes: Any) -> pb.Part:
    part = pb.Part(part_number=FAN, name="Cooler fan capacitor, 25 µF", component_kind="Gearbox",
                   unit="each", fits=[MODEL])  # fmt: skip
    for name, value in changes.items():
        if isinstance(value, list):
            part.ClearField(name)  # type: ignore[arg-type]
            getattr(part, name).extend(value)
        else:
            setattr(part, name, value)
    return part


@dataclass
class World:
    stub: Any
    engine: AsyncEngine

    async def put(self, part: pb.Part, version: int = 0, who: Any = None) -> pb.Part:
        answer = await self.stub.PutPart(
            pb.PutPartRequest(part=part, expected_version=version),
            metadata=who or as_caller(PLANNER), timeout=5,
        )  # fmt: skip
        return answer.part  # type: ignore[no-any-return]

    async def adjust(self, delta: int, part_number: str = FAN, farm: str = FARM,
                     who: Any = None) -> pb.StockLevel:  # fmt: skip
        answer = await self.stub.AdjustStock(
            pb.AdjustStockRequest(part_number=part_number, wind_farm_id=farm, delta=delta,
                                  reason="Delivered"),
            metadata=who or as_caller(PLANNER), timeout=5,
        )  # fmt: skip
        return answer.stock  # type: ignore[no-any-return]

    async def check(self, *lines: tuple[str, int], who: Any = None,
                    farm: str = FARM) -> list[pb.PartCheck]:  # fmt: skip
        answer = await self.stub.CheckParts(
            pb.CheckPartsRequest(model=MODEL, wind_farm_id=farm,
                                 lines=[pb.PartLine(part_number=n, quantity=q) for n, q in lines]),
            metadata=who or as_caller(ENGINEER), timeout=5,
        )  # fmt: skip
        return list(answer.checks)

    async def reserve(self, *lines: tuple[str, int], who: Any = None, key: str = "") -> Any:
        return await self.stub.ReserveParts(
            pb.ReservePartsRequest(work_order_id=WORK_ORDER, wind_farm_id=FARM,
                                   lines=[pb.PartLine(part_number=n, quantity=q)
                                          for n, q in lines], idempotency_key=key),
            metadata=who or as_caller(PLANNER), timeout=5,
        )  # fmt: skip

    async def stock(self, who: Any = None) -> list[pb.StockLevel]:
        answer = await self.stub.GetStock(
            pb.GetStockRequest(part_number=FAN), metadata=who or as_caller(PLANNER), timeout=5
        )
        return list(answer.stock)

    async def events(self, subject: str) -> list[Any]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        return [parse_event(r.subject, r.envelope).payload for r in rows if r.subject == subject]


def with_service(scenario: Callable[[World], Awaitable[None]], *, generous: bool = False) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)

        def register(server: grpc.aio.Server) -> None:
            inventory_pb2_grpc.add_InventoryServiceServicer_to_server(
                InventoryService(engine, FakeAuthorizer(GRANTS, generous), clock=lambda: NOW),
                server,
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = inventory_pb2_grpc.InventoryServiceStub(channel)
                await scenario(World(stub, engine))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_planner_stocks_checks_reserves_and_releases_a_part() -> None:
    """FR-INVT-001: parts, compatibility, stock and reservation status, each change kept in the
    ledger, audited, and announced as evidence.changed."""

    async def scenario(world: World) -> None:
        added = await world.put(fan())
        assert (added.version, added.component_kind) == (1, "gearbox")
        assert (await world.adjust(3)).available == 3
        checked = await world.check((FAN, 2), (FAN + "-X", 1))
        assert [(c.status, c.available) for c in checked] == [("available", 3), ("unknown", 0)]
        held = (await world.reserve((FAN, 2))).reservations
        assert [(r.status, r.quantity, r.reserved_by) for r in held] == [("held", 2, PLANNER)]
        (level,) = await world.stock()
        assert (level.on_hand, level.reserved, level.available) == (3, 2, 1)
        assert [c.status for c in await world.check((FAN, 2))] == ["short"]
        released = (await world.stub.ReleaseReservation(
            pb.ReleaseReservationRequest(reservation_id=held[0].reservation_id,
                                         reason="The work was rescheduled"),
            metadata=as_caller(PLANNER), timeout=5,
        )).reservation  # fmt: skip
        assert (released.status, released.released_by) == ("released", PLANNER)
        assert (await world.stock())[0].available == 3
        listed = await world.stub.ListReservations(
            pb.ListReservationsRequest(work_order_id=WORK_ORDER), metadata=as_caller(PLANNER),
            timeout=5,
        )  # fmt: skip
        assert [r.status for r in listed.reservations] == ["released"]
        async with world.engine.connect() as connection:
            ledger = (await connection.execute(
                select(STOCK_MOVEMENTS).order_by(STOCK_MOVEMENTS.c.position)
            )).all()  # fmt: skip
        assert [(m.kind, m.quantity, m.on_hand, m.reserved) for m in ledger] == [
            ("adjusted", 3, 3, 0), ("reserved", 2, 3, 2), ("released", -2, 3, 0),
        ]  # fmt: skip
        changed = await world.events("evidence.changed")
        assert [(e.source_service, e.record_id, e.version) for e in changed] == [
            ("inventory", f"part:{FAN}", "1"),
            ("inventory", f"stock:{FAN}@{FARM}", "1"),
            ("inventory", f"stock:{FAN}@{FARM}", "2"),
            ("inventory", f"stock:{FAN}@{FARM}", "3"),
        ]
        audited = await world.events("audit.requested.inventory")
        assert [a.action for a in audited] == [
            "inventory.part_added", "inventory.stock_adjusted", "inventory.parts_reserved",
            "inventory.reservation_released",
        ]  # fmt: skip

    with_service(scenario)


def test_compatibility_is_by_model_and_a_retired_part_is_never_available() -> None:
    async def scenario(world: World) -> None:
        await world.put(fan())
        await world.put(pb.Part(part_number="GREASE-EP2", name="Bearing grease", unit="kilogram",
                                component_kind="main_bearing", fits_any_model=True))  # fmt: skip
        await world.put(pb.Part(part_number="GEN-BRG-6330", name="Generator bearing",
                                unit="each", component_kind="generator",
                                fits=[pb.ModelRef(manufacturer="Other", model="X-3")]))  # fmt: skip
        for number in (FAN, "GREASE-EP2", "GEN-BRG-6330"):
            await world.adjust(5, number)
        checked = await world.check((FAN, 1), ("GREASE-EP2", 2), ("GEN-BRG-6330", 1))
        assert [c.status for c in checked] == ["available", "available", "incompatible"]
        # Matched without regard to case or spacing.
        loose = pb.ModelRef(manufacturer="windops  synthetic", model="ws-8.0-167")
        fits = await world.stub.ListParts(pb.ListPartsRequest(fits=loose, wind_farm_id=FARM),
                                          metadata=as_caller(ENGINEER), timeout=5)  # fmt: skip
        assert [(p.part.part_number, p.stock.available) for p in fits.parts] == [
            (FAN, 5), ("GREASE-EP2", 5),
        ]  # fmt: skip
        found = await world.stub.ListParts(pb.ListPartsRequest(query="FAN capacitor"),
                                           metadata=as_caller(ENGINEER), timeout=5)  # fmt: skip
        assert [p.part.part_number for p in found.parts] == [FAN]
        retired = await world.put(fan(retired=True), version=1)
        assert retired.version == 2
        assert [c.status for c in await world.check((FAN, 1))] == ["retired"]
        assert await code_of(world.reserve((FAN, 1))) == grpc.StatusCode.FAILED_PRECONDITION
        assert await code_of(world.adjust(1)) == grpc.StatusCode.FAILED_PRECONDITION
        # Its stock can still be taken away.
        assert (await world.adjust(-5)).on_hand == 0

    with_service(scenario)


def test_an_agent_reads_but_never_changes_or_reserves_even_if_permissions_said_it_may() -> None:
    """FR-INVT-002, FR-INVT-003, SEC-009: an agent's part recommendation is a suggestion. With
    an Authorization that wrongly allowed it everything, this service still refuses."""

    async def scenario(world: World) -> None:
        await world.put(fan())
        await world.adjust(4)
        for agent in (as_caller(PLANNER, actor=AGENT),
                      as_caller("service:fault-investigator", person=False, actor=AGENT),
                      as_caller("service:fault-investigator", person=False)):  # fmt: skip
            assert [c.status for c in await world.check((FAN, 1), who=agent)] == ["available"]
            assert await code_of(world.reserve((FAN, 1), who=agent)) == (
                grpc.StatusCode.PERMISSION_DENIED
            )
            assert await code_of(world.adjust(-1, who=agent)) == grpc.StatusCode.PERMISSION_DENIED
            assert await code_of(world.put(fan(name="Renamed"), version=1, who=agent)) == (
                grpc.StatusCode.PERMISSION_DENIED
            )
        (level,) = await world.stock()
        assert (level.on_hand, level.reserved) == (4, 0)
        # Each refusal is recorded, as Authorization records its own (SRS 9.4, SEC-010).
        refusals = {(a.actor_type, a.actor_id, a.on_behalf_of, a.resource_id)
                    for a in await world.events("audit.requested.inventory")
                    if a.action == "authorization.denied" and a.outcome == "denied"}  # fmt: skip
        assert refusals == {
            ("agent", AGENT, PLANNER, "inventory.reserve"),
            ("agent", AGENT, PLANNER, "inventory.update"),
            ("agent", AGENT, "service:fault-investigator", "inventory.reserve"),
            ("agent", AGENT, "service:fault-investigator", "inventory.update"),
            ("service", "service:fault-investigator", "", "inventory.reserve"),
            ("service", "service:fault-investigator", "", "inventory.update"),
        }

    with_service(scenario, generous=True)


def test_a_reservation_is_every_line_or_none_and_stock_never_drops_below_it() -> None:
    async def scenario(world: World) -> None:
        await world.put(fan())
        await world.put(fan(part_number="GBX-FAN-MOTOR", name="Cooler fan motor"))
        await world.adjust(2)
        await world.adjust(1, "GBX-FAN-MOTOR")
        short = await code_of(world.reserve((FAN, 1), ("GBX-FAN-MOTOR", 2)))
        assert short == grpc.StatusCode.FAILED_PRECONDITION
        assert (await world.stock())[0].reserved == 0
        await world.reserve((FAN, 2))
        assert await code_of(world.adjust(-1)) == grpc.StatusCode.FAILED_PRECONDITION
        nothing = await code_of(world.adjust(-1, "GBX-FAN-MOTOR", farm=ELSEWHERE))
        assert nothing == grpc.StatusCode.FAILED_PRECONDITION

    with_service(scenario)


def test_permissions_by_wind_farm_and_tenant() -> None:
    async def scenario(world: World) -> None:
        await world.put(fan())
        await world.adjust(2)
        await world.adjust(1, farm=ELSEWHERE)
        # Reads the catalog but may not change it.
        assert await code_of(world.put(fan(name="X"), version=1, who=as_caller(ENGINEER))) == (
            grpc.StatusCode.PERMISSION_DENIED
        )
        # No inventory.read at all.
        assert await code_of(world.stub.ListParts(
            pb.ListPartsRequest(), metadata=as_caller(OPERATOR), timeout=5
        )) == grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        # One wind farm only: its own store, and the catalog.
        local = as_caller(LOCAL)
        assert [s.wind_farm_id for s in await world.stock(local)] == [ELSEWHERE]
        assert await code_of(world.check((FAN, 1), who=local)) == (
            grpc.StatusCode.PERMISSION_DENIED
        )
        assert [c.status for c in await world.check((FAN, 1), who=local, farm=ELSEWHERE)] == [
            "available"
        ]
        assert await code_of(world.stub.ListReservations(
            pb.ListReservationsRequest(), metadata=local, timeout=5
        )) == grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        other = await code_of(world.stub.GetPart(
            pb.GetPartRequest(part_number=FAN), metadata=as_caller(PLANNER, tenant=OTHER),
            timeout=5,
        ))  # fmt: skip
        assert other == grpc.StatusCode.NOT_FOUND

    with_service(scenario)


def test_retries_and_versions() -> None:
    async def scenario(world: World) -> None:
        await world.put(fan())
        assert await code_of(world.put(fan())) == grpc.StatusCode.FAILED_PRECONDITION
        assert await code_of(world.put(fan(), version=3)) == grpc.StatusCode.FAILED_PRECONDITION
        await world.adjust(5)
        first = (await world.reserve((FAN, 2), key="same")).reservations
        again = (await world.reserve((FAN, 2), key="same")).reservations
        assert [r.reservation_id for r in again] == [r.reservation_id for r in first]
        assert (await world.stock())[0].reserved == 2
        reused = await code_of(world.reserve((FAN, 1), key="same"))
        assert reused == grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"part_number": "-bad"}, "part_number"),
        ({"unit": "crate"}, "unit"),
        ({"fits": []}, "models it fits"),
        ({"fits_any_model": True}, "names no models"),
        ({"name": " "}, "name"),
    ],
)
def test_each_part_rule_names_what_is_wrong(changes: dict[str, Any], words: str) -> None:
    problem = rules.part_problem(fan(**changes))
    assert words in problem, problem


def test_lines_are_named_once_with_a_quantity() -> None:
    line = pb.PartLine(part_number=FAN, quantity=1)
    assert rules.lines_problem([line]) == ""
    assert "twice" in rules.lines_problem([line, line])
    assert "quantity" in rules.lines_problem([pb.PartLine(part_number=FAN, quantity=0)])
    assert rules.lines_problem([]) == "Name at least one part"
