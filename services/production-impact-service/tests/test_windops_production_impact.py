"""The impact API through a real gRPC server and SQL database, with stand-ins for its owners.
SQLite has no row-level security or triggers, so `make impact-check` proves those on PostgreSQL."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.asset.v1 import asset_pb2
from windops.investigation.v1 import investigation_pb2
from windops.production_impact.v1 import production_impact_pb2 as pb
from windops.production_impact.v1 import production_impact_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_grpc import (
    NotFoundError,
    ServiceRuntime,
    Settings,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_outbox import create_outbox_table
from windops_production_impact.defaults import DEFAULT_REFERENCES, LIBRARY
from windops_production_impact.formulas import Sample
from windops_production_impact.service import ProductionImpactService
from windops_production_impact.store import IMPACT_RESULTS, METADATA, SERVICE_NAME

SECRET = "x" * 32
TENANT = "north-sea"
SETTINGS = Settings(
    environment="test", version="test", identity_secret=SECRET, grpc_port=0, http_port=0,
    shutdown_grace_seconds=1,
)  # fmt: skip
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@dataclass
class FakeAuthorizer:
    grants: set[tuple[str, str]] = field(default_factory=set)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        who = current_identity().user_id or current_identity().subject
        return (who, permission) in self.grants or (
            bool(wind_farm_id) and (who, f"{permission}@{wind_farm_id}") in self.grants
        )


@dataclass
class FakeSources:
    """dogger-east-t01, offshore, stopped for the last hour in a steady 10 m/s wind."""

    model: str = "SG-8.0-167"
    telemetry_down: bool = False
    seconds: int = 3600
    asked_as: list[str] = field(default_factory=list)

    async def case(self, case_id: str) -> Any:
        self.asked_as.append(current_identity().subject)
        if case_id != "case_1":
            raise NotFoundError("no such case")
        return investigation_pb2.Case(case_id="case_1", turbine_id="dogger-east-t01", version=2)

    async def turbine(self, turbine_id: str) -> Any:
        return asset_pb2.Turbine(
            turbine_id=turbine_id, wind_farm_id="dogger-east", version=7,
            model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic", model=self.model,
                                         rated_power_kw=8000, rotor_diameter_m=167),
        )  # fmt: skip

    async def wind_farm(self, wind_farm_id: str) -> Any:
        return asset_pb2.WindFarm(wind_farm_id=wind_farm_id, offshore=True, version=3)

    async def samples(self, turbine_id: str, start: datetime, end: datetime) -> list[Sample]:
        if self.telemetry_down:
            raise UnavailableError("The Telemetry Service cannot be reached right now")
        first = end - timedelta(seconds=self.seconds)
        return [Sample(first + timedelta(seconds=i), 10.0, 0.0) for i in range(self.seconds)]


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    sources: FakeSources


def as_user(user: str = "u-admin", level: str | None = LEVEL_MFA) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=TENANT, user_id=user, auth_level=level,
                                auth_time=int(time.time()) if level else None)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def estimate(world: World, user: str = "u-engineer", key: str = "", **changes: Any) -> Any:
    fields = {"case_id": "case_1", "action": "repair", "downtime_hours": 24.0,
              "idempotency_key": key, **changes}  # fmt: skip
    answer = await world.stub.EstimateImpact(pb.EstimateImpactRequest(**fields),
                                             metadata=as_user(user), timeout=5)  # fmt: skip
    return answer.result


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


def with_service(scenario: Callable[[World], Awaitable[None]], **source: Any) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine_.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        authorizer = FakeAuthorizer({
            ("u-admin", "policy.read"), ("u-admin", "policy.manage"),
            ("u-engineer", "investigation.read"), ("u-dogger", "investigation.read@dogger-east"),
        })  # fmt: skip
        sources = FakeSources(**source)

        def register(server: grpc.aio.Server) -> None:
            production_impact_pb2_grpc.add_ProductionImpactServiceServicer_to_server(
                ProductionImpactService(engine_, authorizer, sources, SECRET, clock=lambda: NOW),
                server,
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = production_impact_pb2_grpc.ProductionImpactServiceStub(channel)
                await scenario(World(stub, engine_, sources))
        finally:
            await runtime.stop()
            await engine_.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_tenant_starts_with_the_defaults_readable_with_policy_read() -> None:
    async def scenario(world: World) -> None:
        listed = await world.stub.ListReferences(pb.ListReferencesRequest(), metadata=as_user(),
                                                 timeout=5)  # fmt: skip
        assert listed.library == LIBRARY
        assert sorted(r.reference_id for r in listed.references) == sorted(
            d[0] for d in DEFAULT_REFERENCES
        )
        refused = world.stub.ListReferences(pb.ListReferencesRequest(),
                                            metadata=as_user("u-engineer"), timeout=5)  # fmt: skip
        assert await code_of(refused) == grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_an_estimate_keeps_its_inputs_formula_assumptions_and_ranges() -> None:
    async def scenario(world: World) -> None:
        result = await estimate(world)
        assert result.result_id.startswith("imp_") and result.verified
        assert result.formula_version == "impact-formulas-1"
        figures = {f.name: f for f in result.figures}
        total = figures["total_cost_eur"]
        assert total.low <= total.value <= total.high and total.unit == "EUR"
        assert figures["maintenance_cost_eur"].value == 24 * 95 + 2 * 14000
        assert figures["observed_availability_pct"].value == 0.0
        inputs = {i.name: i for i in result.inputs}
        assert inputs["site.offshore"].value_json == "true"
        assert inputs["reference.power-curve-sg-8-0-167"].source_ref == ("power-curve-sg-8-0-167@1")
        assert json.loads(inputs["observed.mean_wind_mps"].value_json) == 10.0
        assert len(result.assumptions) >= 6
        assert world.sources.asked_as == ["service:production-impact"]
        read = await world.stub.GetImpactResult(pb.GetImpactResultRequest(
            result_id=result.result_id), metadata=as_user("u-engineer"), timeout=5)  # fmt: skip
        assert read.result == result

    with_service(scenario)


def test_without_telemetry_or_a_power_curve_there_is_no_estimate() -> None:
    async def down(world: World) -> None:
        call = estimate(world)
        assert await code_of(call) == grpc.StatusCode.UNAVAILABLE

    async def unknown_model(world: World) -> None:
        assert await code_of(estimate(world)) == grpc.StatusCode.FAILED_PRECONDITION

    async def too_little(world: World) -> None:
        assert await code_of(estimate(world)) == grpc.StatusCode.FAILED_PRECONDITION

    with_service(down, telemetry_down=True)
    with_service(unknown_model, model="XL-15")
    with_service(too_little, seconds=120)


def test_a_site_price_published_with_a_second_factor_is_used() -> None:
    async def scenario(world: World) -> None:
        await world.stub.ListReferences(pb.ListReferencesRequest(), metadata=as_user(), timeout=5)
        price = {"kind": "energy_price", "eur_per_mwh": 75.0, "sites": {"dogger-east": 150.0}}
        request = pb.PublishReferenceVersionRequest(
            reference_id="energy-price", definition_json=json.dumps(price),
            reason="Dogger East's contract price", expected_version=1, idempotency_key="p2",
        )  # fmt: skip
        password_only = world.stub.PublishReferenceVersion(
            request, metadata=as_user("u-admin", LEVEL_PASSWORD), timeout=5
        )
        assert await code_of(password_only) == grpc.StatusCode.PERMISSION_DENIED
        published = await world.stub.PublishReferenceVersion(request, metadata=as_user(),
                                                             timeout=5)  # fmt: skip
        assert published.reference.current.version == 2
        result = await estimate(world, downtime_hours=1.0)
        revenue = next(f for f in result.figures if f.name == "lost_revenue_eur")
        projected = next(f for f in result.figures if f.name == "projected_lost_energy_mwh")
        assert revenue.value == pytest.approx(projected.value * 150.0, abs=0.01)
        wrong_kind = pb.PublishReferenceVersionRequest(
            reference_id="energy-price", reason="x", expected_version=2,
            definition_json=json.dumps({"kind": "power_curve", "model": {"manufacturer": "M",
                                        "model": "X"}, "points": [[3, 0], [10, 100]],
                                        "tolerance_pct": 5}),
        )  # fmt: skip
        mixed = world.stub.PublishReferenceVersion(wrong_kind, metadata=as_user(), timeout=5)
        assert await code_of(mixed) == grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_who_may_ask_and_a_retry_and_a_changed_row() -> None:
    async def scenario(world: World) -> None:
        stranger = estimate(world, user="u-nobody")
        assert await code_of(stranger) == grpc.StatusCode.NOT_FOUND
        assert await code_of(estimate(world, case_id="case_none")) == grpc.StatusCode.NOT_FOUND
        mine = await estimate(world, user="u-dogger", key="e-1")
        again = await estimate(world, user="u-dogger", key="e-1")
        assert again.result_id == mine.result_id
        assert await code_of(estimate(world, action="fly")) == grpc.StatusCode.INVALID_ARGUMENT
        assert await code_of(estimate(world, downtime_hours=0)) == (
            grpc.StatusCode.INVALID_ARGUMENT
        )
        async with world.engine.begin() as connection:
            await connection.execute(
                update(IMPACT_RESULTS).values(content=IMPACT_RESULTS.c.content + " ")
            )
        listed = await world.stub.ListImpactResults(pb.ListImpactResultsRequest(
            case_id="case_1"), metadata=as_user("u-engineer"), timeout=5)  # fmt: skip
        assert listed.results and not listed.results[0].verified

    with_service(scenario)
