"""The rules API through a real gRPC server and SQL database, with stand-ins for the services it
asks. SQLite has no row-level security or triggers, so `make fault-rules-check` proves those on
PostgreSQL."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.fault_detection.v1 import fault_detection_pb2 as pb
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_events import parse_event
from windops_fault_detection import engine, rules
from windops_fault_detection.defaults import DEFAULT_RULES, LIBRARY
from windops_fault_detection.service import FaultRuleService
from windops_fault_detection.store import DETECTIONS, METADATA, SERVICE_NAME
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, current_identity, sign_identity
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(
    environment="test", version="test", identity_secret=SECRET, grpc_port=0, http_port=0,
    shutdown_grace_seconds=1,
)  # fmt: skip
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
NEW_RULE = {
    "kind": "threshold",
    "parameters": {"metric": "main_bearing_temp_c", "for_seconds": 60},
    "levels": [{"at": 45, "severity": "high"}],
}


@dataclass
class FakeAuthorizer:
    grants: set[tuple[str, str]] = field(default_factory=set)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        user = current_identity().user_id or ""
        return (user, permission) in self.grants or (
            bool(wind_farm_id) and (user, f"{permission}@{wind_farm_id}") in self.grants
        )


@dataclass
class Turbine:
    @dataclass
    class Model:
        rated_power_kw: float = 8000.0
        rotor_diameter_m: float = 167.0

    model: Model = field(default_factory=Model)
    wind_farm_id: str = "dogger-east"


class FakeSources:
    """A turbine whose gearbox oil runs 9 °C hotter than its full load in 10 °C air explains."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, datetime, datetime]] = []
        self.readings: list[str] = []

    async def turbine(self, turbine_id: str) -> Any:
        return Turbine()

    async def samples(
        self, turbine_id: str, start: datetime, end: datetime, readings: list[str]
    ) -> list[Any]:
        self.asked.append((turbine_id, start, end))
        self.readings = readings
        seconds = int((end - start).total_seconds())
        reading = {"power_kw": 8000.0, "ambient_temp_c": 10.0, "gearbox_oil_temp_c": 72.0,
                   "gearbox_bearing_temp_c": 73.0, "main_bearing_temp_c": 32.0,
                   "generator_bearing_temp_c": 63.0, "nacelle_vibration_mm_s": 1.0,
                   "rotor_speed_rpm": 8.0}  # fmt: skip
        return [engine.Sample(start + timedelta(seconds=i + 1), reading) for i in range(seconds)]

    async def alarms(self, turbine_id: str) -> list[Any]:
        return [engine.AlarmView("GBX_OIL_TEMP_HIGH", "gearbox_oil_temp_c", True, NOW)]


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    sources: FakeSources


def as_user(user: str = "u-admin", tenant: str = TENANT, level: str | None = LEVEL_MFA) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=tenant, user_id=user, auth_level=level,
                                auth_time=int(time.time()) if level else None)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


def as_service(tenant: str = TENANT) -> Any:
    identity = VerifiedIdentity(subject="svc-agent-runtime", tenant_id=tenant, user_id=None)
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


async def audited(engine_: AsyncEngine) -> list[tuple[str, str, str]]:
    async with engine_.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE))).all()
    payloads: list[Any] = [parse_event(row.subject, row.envelope).payload for row in rows]
    return [(p.action, p.actor_id, p.resource_id) for p in payloads]


def with_service(scenario: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine_.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        authorizer = FakeAuthorizer({
            ("u-admin", "policy.read"), ("u-admin", "policy.manage"),
            ("u-engineer", "policy.read"), ("u-other-admin", "policy.read"),
            ("u-engineer", "investigation.read"),
            ("u-dogger", "investigation.read@dogger-east"),
        })  # fmt: skip
        sources = FakeSources()

        def register(server: grpc.aio.Server) -> None:
            fault_detection_pb2_grpc.add_FaultRuleServiceServicer_to_server(
                FaultRuleService(engine_, authorizer, sources, clock=lambda: NOW), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = fault_detection_pb2_grpc.FaultRuleServiceStub(channel)
                await scenario(World(stub, engine_, sources))
        finally:
            await runtime.stop()
            await engine_.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_tenant_starts_with_the_default_library_as_version_1_of_each_audited() -> None:
    async def scenario(world: World) -> None:
        listed = await world.stub.ListFaultRules(
            pb.ListFaultRulesRequest(), metadata=as_user("u-engineer"), timeout=5
        )
        assert listed.library == LIBRARY
        assert [r.rule_id for r in listed.rules] == sorted(d[0] for d in DEFAULT_RULES)
        assert {(r.current.version, r.current.created_by, r.enabled) for r in listed.rules} == {
            (1, "system", True)
        }
        assert all(len(r.current.content_hash) == 64 for r in listed.rules)
        assert {a for a, _, _ in await audited(world.engine)} == {"fault_rule.created"}
        # Asked again, nothing is installed twice.
        await world.stub.ListFaultRules(pb.ListFaultRulesRequest(), metadata=as_user(), timeout=5)
        assert len(await audited(world.engine)) == len(DEFAULT_RULES)

    with_service(scenario)


def test_only_a_person_with_policy_manage_and_a_recent_second_factor_changes_a_rule() -> None:
    """FR-FLT-005: an agent, or any service, cannot change a rule or its severity."""

    async def scenario(world: World) -> None:
        request = pb.PublishFaultRuleVersionRequest(
            rule_id="main-bearing-hot",
            name="Main bearing hot",
            reason="Site limit",
            definition_json=json.dumps(NEW_RULE),
        )
        for metadata, refused in (
            (as_user("u-engineer"), grpc.StatusCode.PERMISSION_DENIED),
            (as_user("u-admin", level=LEVEL_PASSWORD), grpc.StatusCode.PERMISSION_DENIED),
            (as_service(), grpc.StatusCode.PERMISSION_DENIED),
        ):
            assert (
                await code_of(
                    world.stub.PublishFaultRuleVersion(request, metadata=metadata, timeout=5)
                )
                == refused
            )
        made = await world.stub.PublishFaultRuleVersion(request, metadata=as_user(), timeout=5)
        assert (made.rule.current.version, made.rule.current.created_by) == (1, "u-admin")
        assert ("fault_rule.created", "u-admin", "main-bearing-hot") in await audited(world.engine)

    with_service(scenario)


def test_each_change_is_a_new_version_and_the_old_one_stays() -> None:
    """FR-FLT-002: versioned, with who, when and why, and refused if someone changed it first."""

    async def scenario(world: World) -> None:
        current = await world.stub.GetFaultRule(
            pb.GetFaultRuleRequest(rule_id="vibration-high"), metadata=as_user(), timeout=5
        )
        definition = json.loads(current.rule.current.definition_json)
        definition["levels"] = [{"at": 3.0, "severity": "medium"}, {"at": 3.6, "severity": "high"}]
        request = pb.PublishFaultRuleVersionRequest(
            rule_id="vibration-high",
            definition_json=json.dumps(definition),
            reason="Raised after the site survey",
            expected_version=1,
            idempotency_key="v2",
        )
        first = await world.stub.PublishFaultRuleVersion(request, metadata=as_user(), timeout=5)
        again = await world.stub.PublishFaultRuleVersion(request, metadata=as_user(), timeout=5)
        assert first == again and first.rule.current.version == 2
        history = await world.stub.GetFaultRule(
            pb.GetFaultRuleRequest(rule_id="vibration-high"), metadata=as_user(), timeout=5
        )
        assert [(v.version, v.created_by, v.reason) for v in history.versions] == [
            (1, "system", f"The default library, {LIBRARY}"),
            (2, "u-admin", "Raised after the site survey"),
        ]
        assert history.versions[0].definition_json == current.rule.current.definition_json
        stale = pb.PublishFaultRuleVersionRequest(
            rule_id="vibration-high",
            definition_json=json.dumps(definition),
            reason="Mine",
            expected_version=1,
        )
        assert (
            await code_of(world.stub.PublishFaultRuleVersion(stale, metadata=as_user(), timeout=5))
            == grpc.StatusCode.FAILED_PRECONDITION
        )
        broken = pb.PublishFaultRuleVersionRequest(
            rule_id="vibration-high",
            reason="x",
            expected_version=2,
            definition_json=json.dumps({**definition, "levels": [{"at": 1, "severity": "huge"}]}),
        )
        assert (
            await code_of(world.stub.PublishFaultRuleVersion(broken, metadata=as_user(), timeout=5))
            == grpc.StatusCode.INVALID_ARGUMENT
        )

    with_service(scenario)


def test_a_disabled_rule_finds_nothing_and_a_dry_run_reports_what_the_others_find() -> None:
    async def scenario(world: World) -> None:
        dry = pb.EvaluateTurbineRequest(turbine_id="dogger-east-t01")
        result = await world.stub.EvaluateTurbine(dry, metadata=as_user("u-engineer"), timeout=10)
        found = {f.rule_id: f for f in result.findings}
        oil = found["gearbox-oil-residual"]
        assert (oil.severity, oil.version) == ("high", 1)
        assert json.loads(oil.observed_json)["least_residual"] == 9.0
        assert "gearbox-oil-alarm-and-residual" in found, "the alarm, and the residual"
        (asked,) = world.sources.asked
        defaults = [rules.parse(rule_id, 1, name, d) for rule_id, name, _, d in DEFAULT_RULES]
        # Exactly as much telemetry as the rules need, up to the moment asked about.
        assert asked[2] == NOW and asked[2] - asked[1] == engine.lookback(defaults)
        assert world.sources.readings == [
            "ambient_temp_c", "gearbox_bearing_temp_c", "gearbox_oil_temp_c",
            "generator_bearing_temp_c", "main_bearing_temp_c", "nacelle_vibration_mm_s",
            "power_kw", "rotor_speed_rpm",
        ]  # fmt: skip
        off = pb.SetFaultRuleEnabledRequest(rule_id="gearbox-oil-residual", enabled=False,
                                            reason="Sensor work")  # fmt: skip
        await world.stub.SetFaultRuleEnabled(off, metadata=as_user(), timeout=5)
        again = await world.stub.EvaluateTurbine(dry, metadata=as_user("u-engineer"), timeout=10)
        assert "gearbox-oil-residual" not in {f.rule_id for f in again.findings}
        # Its correlation names it, so that goes quiet too.
        assert "gearbox-oil-alarm-and-residual" not in {f.rule_id for f in again.findings}
        assert ("fault_rule.disabled", "u-admin", "gearbox-oil-residual") in await audited(
            world.engine
        )

    with_service(scenario)


def test_another_tenant_has_its_own_rules() -> None:
    async def scenario(world: World) -> None:
        mine = pb.PublishFaultRuleVersionRequest(
            rule_id="main-bearing-hot",
            name="Main bearing hot",
            reason="Site limit",
            definition_json=json.dumps(NEW_RULE),
        )
        await world.stub.PublishFaultRuleVersion(mine, metadata=as_user(), timeout=5)
        theirs = await world.stub.ListFaultRules(
            pb.ListFaultRulesRequest(), metadata=as_user("u-other-admin", OTHER), timeout=5
        )
        assert "main-bearing-hot" not in {r.rule_id for r in theirs.rules}
        assert len(theirs.rules) == len(DEFAULT_RULES)

    with_service(scenario)


def test_detections_are_read_by_those_who_may_read_investigations() -> None:
    async def scenario(world: World) -> None:
        async with world.engine.begin() as connection:
            await connection.execute(
                DETECTIONS.insert().values(
                    tenant_id=TENANT, detection_id="det_1", turbine_id="dogger-east-t01",
                    rule_id="gearbox-oil-residual", rule_version=1, rule_content_hash="h" * 64,
                    kind="residual", severity="high", amount=9.0, summary="Hot",
                    observed="{}", since=NOW, detected_at=NOW, last_seen_at=NOW,
                    window_start=NOW - timedelta(hours=2), window_end=NOW,
                    telemetry_snapshot_id="tsnap_1", telemetry_snapshot_hash="s" * 64,
                    alarm_ids='["alm_1"]', state="open",
                )
            )  # fmt: skip
        listed = await world.stub.ListDetections(
            pb.ListDetectionsRequest(), metadata=as_user("u-engineer"), timeout=5
        )
        (found,) = listed.detections
        assert (found.detection_id, found.telemetry_snapshot_id, list(found.alarm_ids)) == (
            "det_1", "tsnap_1", ["alm_1"],
        )  # fmt: skip
        get = pb.GetDetectionRequest(detection_id="det_1")
        # Scoped to the turbine's wind farm, a reader sees it, by turbine.
        scoped = await world.stub.GetDetection(get, metadata=as_user("u-dogger"), timeout=5)
        assert scoped.detection.rule_id == "gearbox-oil-residual"
        everything = pb.ListDetectionsRequest()
        assert (
            await code_of(
                world.stub.ListDetections(everything, metadata=as_user("u-dogger"), timeout=5)
            )
            == grpc.StatusCode.PERMISSION_DENIED
        )
        assert (
            await code_of(world.stub.GetDetection(get, metadata=as_user("u-admin"), timeout=5))
            == grpc.StatusCode.NOT_FOUND
        )
        assert (
            await code_of(
                world.stub.GetDetection(get, metadata=as_user("u-engineer", OTHER), timeout=5)
            )
            == grpc.StatusCode.NOT_FOUND
        )

    with_service(scenario)
