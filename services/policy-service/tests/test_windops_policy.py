"""The policy API through a real gRPC server and SQL database, with stand-ins for its owners.
SQLite has no row-level security or triggers, so `make policy-check` proves those on PostgreSQL."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.asset.v1 import asset_pb2
from windops.investigation.v1 import investigation_pb2
from windops.policy.v1 import policy_pb2 as pb
from windops.policy.v1 import policy_pb2_grpc
from windops.production_impact.v1.production_impact_pb2 import Figure, ImpactResult
from windops.weather.v1.weather_pb2 import Forecast, ForecastSummary, GetForecastResponse
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
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
from windops_outbox import OUTBOX_TABLE, create_outbox_table
from windops_policy import library
from windops_policy.defaults import DEFAULT_RULES, LIBRARY, UPGRADES
from windops_policy.service import PolicyService
from windops_policy.store import METADATA, POLICY_RESULTS, POLICY_RULES, SERVICE_NAME

SECRET = "x" * 32
TENANT = "north-sea"
SETTINGS = Settings(
    environment="test", version="test", identity_secret=SECRET, grpc_port=0, http_port=0,
    shutdown_grace_seconds=1,
)  # fmt: skip
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def stamp(at: datetime) -> Timestamp:
    value = Timestamp()
    value.FromDatetime(at)
    return value


@dataclass
class FakeAuthorizer:
    grants: set[tuple[str, str]] = field(default_factory=set)
    asked_as: list[str] = field(default_factory=list)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        who = current_identity().user_id or current_identity().subject
        self.asked_as.append(who)
        return (who, permission) in self.grants or (
            bool(wind_farm_id) and (who, f"{permission}@{wind_farm_id}") in self.grants
        )


@dataclass
class FakeSources:
    """A high-severity open case on dogger-east-t01, its evidence frozen two hours ago."""

    case_: Any = None
    evidence_age: timedelta = timedelta(hours=2)
    turbine_status: str = "operating"
    asset_down: bool = False
    # The Production Impact estimate: the high end of its cost range, and its downtime.
    impact_cost_high: float = 32000.0
    impact_downtime: float = 24.0
    impact_case: str = "case_1"
    # The Weather Service's answer for the planned window.
    weather_state: str = "fresh"
    weather_provider: str = "open-meteo"
    hub_wind: float = 10.0
    gusts: float = 15.0
    hours_covered: int = 4
    weather_age: timedelta = timedelta(minutes=20)
    asked_as: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.case_ = investigation_pb2.Case(
            case_id="case_1", turbine_id="dogger-east-t01", status="open", severity="high",
            evidence_status="ready", evidence_snapshot_id="evs_1", version=3,
        )  # fmt: skip

    async def case(self, case_id: str) -> Any:
        self.asked_as.append(current_identity().subject)
        if case_id != self.case_.case_id:
            raise NotFoundError("no such case")
        return self.case_

    async def evidence(self, snapshot_id: str) -> Any:
        return investigation_pb2.EvidenceSnapshot(
            snapshot_id=snapshot_id, version=1, created_at=stamp(NOW - self.evidence_age)
        )

    async def turbine(self, turbine_id: str) -> Any:
        if self.asset_down:
            raise UnavailableError("The Asset Service cannot be reached right now")
        return asset_pb2.Turbine(turbine_id=turbine_id, wind_farm_id="dogger-east",
                                 status=self.turbine_status, version=7)  # fmt: skip

    async def forecast(self, turbine_id: str, start: datetime, end: datetime) -> Any:
        wanted = int((end - start).total_seconds() // 3600)
        if self.weather_state == "unavailable":
            return GetForecastResponse(state="unavailable", note="the provider cannot be reached")
        return GetForecastResponse(
            state=self.weather_state, note="" if self.weather_state == "fresh" else "4.0 h old",
            forecast=Forecast(forecast_id="fct_" + "b" * 20, provider=self.weather_provider,
                              retrieved_at=stamp(NOW - self.weather_age), hub_height_m=109.0),
            summary=ForecastSummary(max_wind_speed_hub_mps=self.hub_wind,
                                    max_wind_gusts_10m_mps=self.gusts,
                                    hours_covered=min(self.hours_covered, wanted),
                                    hours_wanted=wanted),
        )  # fmt: skip

    async def impact(self, result_id: str) -> Any:
        return ImpactResult(
            result_id=result_id, case_id=self.impact_case, downtime_hours=self.impact_downtime,
            verified=True, estimated_at=stamp(NOW - timedelta(minutes=5)),
            figures=[
                Figure(name="total_cost_eur", value=self.impact_cost_high * 0.9,
                       low=self.impact_cost_high * 0.8, high=self.impact_cost_high),
                Figure(name="projected_lost_energy_mwh", value=40.0, low=35.0, high=45.0),
            ],
        )  # fmt: skip


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    sources: FakeSources
    authorizer: FakeAuthorizer


def as_user(user: str = "u-admin", level: str | None = LEVEL_MFA) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=TENANT, user_id=user, auth_level=level,
                                auth_time=int(time.time()) if level else None)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


def proposal(**changes: Any) -> pb.Proposal:
    fields: dict[str, Any] = {
        "action": "inspection", "summary": "Inspect the gearbox oil cooler",
        "requires_climb": False, "estimated_cost_eur": 2000.0,
        "estimated_downtime_hours": 4.0, "confidence": 0.8,
    }  # fmt: skip
    fields.update(changes)
    return pb.Proposal(**{k: v for k, v in fields.items() if v is not None})


IMPACT_ID = "imp_" + "a" * 20


async def evaluate(world: World, user: str = "u-engineer", key: str = "",
                   impact: str = IMPACT_ID, **changes: Any) -> Any:  # fmt: skip
    request = pb.EvaluateProposalRequest(case_id="case_1", proposal=proposal(**changes),
                                         idempotency_key=key, impact_result_id=impact)  # fmt: skip
    return (await world.stub.EvaluateProposal(request, metadata=as_user(user), timeout=5)).result


def by_rule(result: Any) -> dict[str, str]:
    return {r.rule_id: r.outcome for r in result.rules}


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


async def audited(engine_: AsyncEngine) -> list[tuple[str, str, str, str]]:
    async with engine_.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE))).all()
    payloads: list[Any] = [parse_event(row.subject, row.envelope).payload for row in rows]
    return [(p.action, p.actor_id, p.resource_id, p.outcome) for p in payloads]


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
            policy_pb2_grpc.add_PolicyServiceServicer_to_server(
                PolicyService(engine_, authorizer, sources, SECRET, clock=lambda: NOW), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = policy_pb2_grpc.PolicyServiceStub(channel)
                await scenario(World(stub, engine_, sources, authorizer))
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
        listed = await world.stub.ListPolicyRules(pb.ListPolicyRulesRequest(),
                                                  metadata=as_user(), timeout=5)  # fmt: skip
        assert listed.library == LIBRARY
        assert sorted(r.rule_id for r in listed.rules) == sorted(d[0] for d in DEFAULT_RULES)
        assert all(r.current.version == 1 and r.current.created_by == "system" and r.enabled
                   for r in listed.rules)  # fmt: skip
        created = [a for a in await audited(world.engine) if a[0] == "policy_rule.created"]
        assert len(created) == len(DEFAULT_RULES)
        refused = world.stub.ListPolicyRules(pb.ListPolicyRulesRequest(),
                                             metadata=as_user("u-engineer"), timeout=5)  # fmt: skip
        assert await code_of(refused) == grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_a_sound_proposal_passes_with_every_rule_version_and_input_recorded_and_audited() -> None:
    async def scenario(world: World) -> None:
        result = await evaluate(world)
        assert result.outcome == "pass", by_rule(result)
        assert result.result_id.startswith("pol_") and result.verified and result.content_hash
        assert (result.case_id, result.turbine_id, result.wind_farm_id) == (
            "case_1", "dogger-east-t01", "dogger-east",
        )  # fmt: skip
        outcomes = by_rule(result)
        assert outcomes["work-at-height-wind"] == "not_applicable"
        assert all(r.version == 1 and len(r.content_hash) == 64 for r in result.rules)
        inputs = {i.name: i for i in result.inputs}
        assert (inputs["case.severity"].value_json, inputs["case.severity"].source_ref) == (
            '"high"', "case_1@3",
        )  # fmt: skip
        assert json.loads(inputs["evidence.age_hours"].value_json) == 2.0
        # A proposal on the ground, with no planned window, has no forecast to judge.
        assert inputs["weather.max_wind_speed_mps"].state == "missing"
        # Gathered as the service itself, while the caller's own permission was checked.
        assert world.sources.asked_as == ["service:policy"]
        assert "u-engineer" in world.authorizer.asked_as
        read = await world.stub.GetPolicyResult(pb.GetPolicyResultRequest(
            result_id=result.result_id), metadata=as_user("u-engineer"), timeout=5)  # fmt: skip
        assert read.result == result
        assert ("policy.evaluated", "u-engineer", result.result_id, "pass") in await audited(
            world.engine
        )

    with_service(scenario)


async def climb(world: World) -> Any:
    return await evaluate(world, requires_climb=True, planned_start=stamp(NOW + timedelta(hours=2)),
                          planned_end=stamp(NOW + timedelta(hours=6)))  # fmt: skip


def rule_of(result: Any, rule_id: str) -> Any:
    return next(r for r in result.rules if r.rule_id == rule_id)


def test_work_at_height_is_judged_on_the_forecast_hub_wind_and_10_m_gusts() -> None:
    async def scenario(world: World) -> None:
        calm = await climb(world)
        assert calm.outcome == "pass", by_rule(calm)
        inputs = {i.name: i for i in calm.inputs}
        wind = inputs["weather.max_wind_speed_mps"]
        assert (wind.source, wind.source_ref, json.loads(wind.value_json)) == (
            "weather", "open-meteo:fct_" + "b" * 20, 10.0,
        )  # fmt: skip
        assert "109 m hub" in wind.note and "10 m" in inputs["weather.max_gust_mps"].note
        world.sources.hub_wind = 12.0
        assert (await climb(world)).outcome == "pass", "at the limit passes"
        world.sources.hub_wind = 12.01
        windy = await climb(world)
        assert (windy.outcome, by_rule(windy)["work-at-height-wind"]) == ("block", "block")
        world.sources.hub_wind, world.sources.gusts = 10.0, 18.5
        assert by_rule(await climb(world))["work-at-height-gusts"] == "block"

    with_service(scenario)


def test_work_at_height_blocks_on_weather_that_cannot_be_used() -> None:
    async def scenario(world: World) -> None:
        for change, why in (
            ({"weather_state": "stale"}, "stale"),
            ({"weather_state": "unavailable"}, "unavailable"),
            ({"hours_covered": 3}, "covers 3 of the 4 hours"),
            ({"weather_provider": "tavily"}, "not a structured forecast provider"),
            ({"weather_age": timedelta(hours=3, seconds=1)}, "stale"),
        ):
            before = {name: getattr(world.sources, name) for name in change}
            for name, value in change.items():
                setattr(world.sources, name, value)
            wind = rule_of(await climb(world), "work-at-height-wind")
            assert wind.outcome == "block" and why in wind.reason, (change, wind.reason)
            for name, value in before.items():
                setattr(world.sources, name, value)
        no_window = rule_of(await evaluate(world, requires_climb=True), "work-at-height-wind")
        assert no_window.outcome == "block" and "no planned window" in no_window.reason
        on_the_ground = rule_of(await evaluate(world), "work-at-height-wind")
        assert on_the_ground.outcome == "not_applicable"

    with_service(scenario)


def test_each_risk_and_business_limit_escalates() -> None:
    async def scenario(world: World) -> None:
        for changes, rule in (
            ({"confidence": 0.59}, "low-confidence"),
            ({"confidence": None}, "low-confidence"),
        ):
            result = await evaluate(world, **changes)
            assert (result.outcome, by_rule(result)[rule]) == ("escalate", "escalate"), changes
        # Cost and downtime come from the estimate, judged at the high end of its range.
        world.sources.impact_cost_high = 50000.01
        assert by_rule(await evaluate(world))["cost-limit"] == "escalate"
        world.sources.impact_cost_high, world.sources.impact_downtime = 50000.0, 48.5
        assert by_rule(await evaluate(world))["downtime-limit"] == "escalate"
        world.sources.impact_downtime = 48.0
        at_limit = await evaluate(world, confidence=0.6)
        assert at_limit.outcome == "pass", by_rule(at_limit)
        # The caller's own figures are kept, and judge nothing.
        stated = await evaluate(world, estimated_cost_eur=9e9, estimated_downtime_hours=9e5)
        assert stated.outcome == "pass"

    with_service(scenario)


def test_a_critical_case_escalates_and_closed_case_or_old_evidence_blocks() -> None:
    async def scenario(world: World) -> None:
        world.sources.case_.severity = "critical"
        assert by_rule(await evaluate(world))["critical-case"] == "escalate"
        world.sources.case_.severity, world.sources.case_.status = "high", "closed"
        closed = await evaluate(world)
        assert (closed.outcome, by_rule(closed)["case-active"]) == ("block", "block")
        world.sources.case_.status, world.sources.case_.evidence_status = "open", "pending"
        assert by_rule(await evaluate(world))["evidence-ready"] == "block"
        world.sources.case_.evidence_status, world.sources.evidence_age = (
            "ready",
            timedelta(hours=25),
        )
        stale = await evaluate(world)
        assert (stale.outcome, by_rule(stale)["evidence-fresh"]) == ("block", "block")

    with_service(scenario)


def test_an_owner_that_cannot_be_reached_fails_closed() -> None:
    async def scenario(world: World) -> None:
        result = await evaluate(world)
        assert result.outcome == "escalate" and result.wind_farm_id == ""
        offline = next(r for r in result.rules if r.rule_id == "turbine-offline")
        assert offline.outcome == "escalate" and "unavailable" in offline.reason

    with_service(scenario, asset_down=True)


def test_a_site_gets_its_own_limit_from_a_new_rule_version() -> None:
    async def scenario(world: World) -> None:
        await world.stub.ListPolicyRules(pb.ListPolicyRulesRequest(), metadata=as_user(),
                                         timeout=5)  # fmt: skip
        definition = next(d for r, _, _, d in DEFAULT_RULES if r == "cost-limit")
        stricter = {**definition, "parameters": {**definition["parameters"],
                                                 "sites": {"dogger-east": 10000.0}}}  # fmt: skip
        publish = pb.PublishPolicyRuleVersionRequest(
            rule_id="cost-limit", definition_json=json.dumps(stricter),
            reason="Dogger East's approval limit", expected_version=1, idempotency_key="v2",
        )  # fmt: skip
        published = await world.stub.PublishPolicyRuleVersion(publish, metadata=as_user(),
                                                               timeout=5)  # fmt: skip
        assert published.rule.current.version == 2
        world.sources.impact_cost_high = 20000.0
        result = await evaluate(world)
        cost = next(r for r in result.rules if r.rule_id == "cost-limit")
        assert (cost.outcome, cost.version, cost.limit_json, cost.limit_from) == (
            "escalate", 2, "10000.0", "dogger-east",
        )  # fmt: skip

    with_service(scenario)


def test_only_a_person_with_policy_manage_and_a_recent_second_factor_changes_a_rule() -> None:
    async def scenario(world: World) -> None:
        definition = json.dumps(next(d for r, _, _, d in DEFAULT_RULES if r == "cost-limit"))

        def publish(user: str, level: str | None = LEVEL_MFA, **change: Any) -> Any:
            fields = {"rule_id": "cost-limit", "definition_json": definition, "reason": "Review",
                      "expected_version": 1, **change}  # fmt: skip
            return world.stub.PublishPolicyRuleVersion(
                pb.PublishPolicyRuleVersionRequest(**fields),
                metadata=as_user(user, level),
                timeout=5,
            )

        assert await code_of(publish("u-engineer")) == grpc.StatusCode.PERMISSION_DENIED
        assert (
            await code_of(publish("u-admin", LEVEL_PASSWORD)) == grpc.StatusCode.PERMISSION_DENIED
        )
        open_ended = json.dumps({**json.loads(definition), "when_missing": "pass"})
        assert await code_of(publish("u-admin", definition_json=open_ended)) == (
            grpc.StatusCode.INVALID_ARGUMENT
        )
        stale = publish("u-admin", expected_version=5)
        assert await code_of(stale) == grpc.StatusCode.FAILED_PRECONDITION

    with_service(scenario)


def test_someone_who_may_not_read_the_case_cannot_find_it_and_a_site_grant_is_enough() -> None:
    async def scenario(world: World) -> None:
        stranger = world.stub.EvaluateProposal(
            pb.EvaluateProposalRequest(case_id="case_1", proposal=proposal()),
            metadata=as_user("u-nobody"), timeout=5,
        )  # fmt: skip
        assert await code_of(stranger) == grpc.StatusCode.NOT_FOUND
        missing = world.stub.EvaluateProposal(
            pb.EvaluateProposalRequest(case_id="case_none", proposal=proposal()),
            metadata=as_user("u-engineer"), timeout=5,
        )  # fmt: skip
        assert await code_of(missing) == grpc.StatusCode.NOT_FOUND
        mine = await evaluate(world, user="u-dogger")
        assert mine.outcome == "pass"
        read = world.stub.GetPolicyResult(pb.GetPolicyResultRequest(result_id=mine.result_id),
                                          metadata=as_user("u-nobody"), timeout=5)  # fmt: skip
        assert await code_of(read) == grpc.StatusCode.NOT_FOUND

    with_service(scenario)


def test_a_retry_returns_the_first_result_and_a_changed_row_no_longer_verifies() -> None:
    async def scenario(world: World) -> None:
        first = await evaluate(world, key="eval-1")
        again = await evaluate(world, key="eval-1")
        assert again.result_id == first.result_id
        reused = world.stub.EvaluateProposal(
            pb.EvaluateProposalRequest(case_id="case_1", proposal=proposal(confidence=0.9),
                                       idempotency_key="eval-1"),
            metadata=as_user("u-engineer"), timeout=5,
        )  # fmt: skip
        assert await code_of(reused) == grpc.StatusCode.INVALID_ARGUMENT
        async with world.engine.begin() as connection:
            await connection.execute(
                update(POLICY_RESULTS).values(content=POLICY_RESULTS.c.content + " ")
            )
        listed = await world.stub.ListPolicyResults(pb.ListPolicyResultsRequest(
            case_id="case_1"), metadata=as_user("u-engineer"), timeout=5)  # fmt: skip
        assert [r.result_id for r in listed.results] == [first.result_id]
        assert not listed.results[0].verified

    with_service(scenario)


def test_a_proposal_that_cannot_be_judged_as_stated_is_refused() -> None:
    async def scenario(world: World) -> None:
        for changes in ({"action": "fly"}, {"confidence": 1.5}, {"estimated_cost_eur": -1.0},
                        {"planned_start": stamp(NOW)}):  # fmt: skip
            call = world.stub.EvaluateProposal(
                pb.EvaluateProposalRequest(case_id="case_1", proposal=proposal(**changes)),
                metadata=as_user("u-engineer"), timeout=5,
            )  # fmt: skip
            assert await code_of(call) == grpc.StatusCode.INVALID_ARGUMENT, changes

    with_service(scenario)


def test_without_a_usable_estimate_cost_and_downtime_escalate() -> None:
    async def scenario(world: World) -> None:
        none = await evaluate(world, impact="")
        assert (by_rule(none)["cost-limit"], by_rule(none)["downtime-limit"]) == (
            "escalate", "escalate",
        )  # fmt: skip
        cost = next(r for r in none.rules if r.rule_id == "cost-limit")
        assert "no Production Impact estimate" in cost.reason
        world.sources.impact_case = "case_other"
        other = next(r for r in (await evaluate(world)).rules if r.rule_id == "cost-limit")
        assert other.outcome == "escalate" and "another case" in other.reason
        refused = world.stub.EvaluateProposal(
            pb.EvaluateProposalRequest(case_id="case_1", proposal=proposal(),
                                       impact_result_id="pol_wrong"),
            metadata=as_user("u-engineer"), timeout=5,
        )  # fmt: skip
        assert await code_of(refused) == grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_rules_still_on_the_systems_first_defaults_move_and_a_persons_own_stay() -> None:
    async def run() -> None:
        engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine_.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
            for rule_id, definition in UPGRADES.items():
                by = "system" if rule_id == "cost-limit" else "u-admin"
                await library.write_version(connection, TENANT, rule_id, 1, definition, by,
                                            "policy-rules-1", NOW)  # fmt: skip
                await connection.execute(POLICY_RULES.insert().values(
                    tenant_id=TENANT, rule_id=rule_id, name=rule_id, description="",
                    enabled=True, current_version=1, created_at=NOW, updated_at=NOW,
                ))  # fmt: skip
        await library.install_defaults(engine_, TENANT, NOW)
        async with engine_.connect() as connection:
            enabled = await library.enabled_rules(connection, TENANT)
        found = {rule.rule_id: rule for rule, _ in enabled}
        assert (found["cost-limit"].version, found["cost-limit"].input) == (
            2, "impact.total_cost_eur",
        )  # fmt: skip
        assert (found["downtime-limit"].version, found["downtime-limit"].input) == (
            1, "proposal.estimated_downtime_hours",
        ), "a rule a person changed stays theirs"  # fmt: skip
        await engine_.dispose()

    asyncio.run(run())
