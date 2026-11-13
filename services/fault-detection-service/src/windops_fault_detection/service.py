"""The gRPC API: fault rules, each version kept, and a dry run of them against one turbine.

Every call asks Authorization first, and stays in the caller's tenant. A dry run reads the
turbine, its telemetry and its alarms from their owners, as the caller, so it sees only what they
may see. It records nothing: M5.3 records detections.
"""

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.alarm.v1 import alarm_pb2
from windops.asset.v1 import asset_pb2
from windops.authorization.v1 import authorization_pb2
from windops.fault_detection.v1 import fault_detection_pb2 as pb
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2
from windops_auth import (
    STEP_UP_MESSAGE,
    MfaRequiredError,
    authentication_claims,
    require_recent_mfa,
)
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ServiceClient,
    UnavailableError,
    current_identity,
)
from windops_migrations import in_tenant
from windops_resilience import BreakerOpenError

from windops_fault_detection import engine, rules
from windops_fault_detection.defaults import DEFAULT_RULES, LIBRARY
from windops_fault_detection.store import (
    FAULT_RULE_VERSIONS,
    FAULT_RULES,
    KeyReusedError,
    audit,
    remember,
    remembered,
)

READ, MANAGE = "policy.read", "policy.manage"
DEADLINE_SECONDS = 3.0
# A dry run streams up to a few hours of every reading.
STREAM_DEADLINE_SECONDS = 30.0
MAXIMUM_KEY, MAXIMUM_TEXT, MAXIMUM_DEFINITION = 64, 500, 8000
SYSTEM = "system"
_RULE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
# Each telemetry metric, by the reading it is.
READINGS = {
    telemetry_pb2.METRIC_WIND_SPEED: "wind_speed_mps",
    telemetry_pb2.METRIC_WIND_DIRECTION: "wind_direction_deg",
    telemetry_pb2.METRIC_ROTOR_SPEED: "rotor_speed_rpm",
    telemetry_pb2.METRIC_GENERATOR_SPEED: "generator_speed_rpm",
    telemetry_pb2.METRIC_POWER: "power_kw",
    telemetry_pb2.METRIC_PITCH_ANGLE: "pitch_angle_deg",
    telemetry_pb2.METRIC_AMBIENT_TEMPERATURE: "ambient_temp_c",
    telemetry_pb2.METRIC_GEARBOX_TEMPERATURE: "gearbox_oil_temp_c",
    telemetry_pb2.METRIC_BEARING_TEMPERATURE: "gearbox_bearing_temp_c",
    telemetry_pb2.METRIC_MAIN_BEARING_TEMPERATURE: "main_bearing_temp_c",
    telemetry_pb2.METRIC_GENERATOR_BEARING_TEMPERATURE: "generator_bearing_temp_c",
    telemetry_pb2.METRIC_VIBRATION_RMS: "nacelle_vibration_mm_s",
}


class Authorizer(Protocol):
    async def allowed(self, permission: str) -> bool: ...


class Sources(Protocol):
    """What a dry run reads, each from its owner, as the caller."""

    async def turbine(self, turbine_id: str) -> Any: ...
    async def samples(
        self, turbine_id: str, start: datetime, end: datetime, readings: list[str]
    ) -> list[Any]: ...
    async def alarms(self, turbine_id: str) -> list[Any]: ...


async def _call(client: ServiceClient, method: Any, request: Message, who: str,
                deadline: float = DEADLINE_SECONDS) -> Any:  # fmt: skip
    try:
        return await client.call(method, request, idempotent=True, deadline_seconds=deadline)
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError("No such turbine") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str) -> bool:
        request = authorization_pb2.CheckRequest(permission=permission)
        try:
            answer = await _call(self._client, self._stub.Check, request, "Authorization")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)


def _collected(method: Callable[..., Any]) -> Callable[..., Awaitable[list[Any]]]:
    """A stream as one awaited call, so it passes the same breaker and deadline as any other."""

    async def collect(request: Any, *, timeout: float, metadata: Any) -> list[Any]:
        return [item async for item in method(request, timeout=timeout, metadata=metadata)]

    return collect


def _stamp(value: datetime) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


class ServiceSources:
    def __init__(self, asset: Any, telemetry: Any, alarm: Any, clients: dict[str, Any]) -> None:
        self._asset, self._telemetry, self._alarm, self._clients = asset, telemetry, alarm, clients

    async def turbine(self, turbine_id: str) -> Any:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        answer = await _call(self._clients["asset"], self._asset.GetTurbine, request, "Asset")
        return answer.turbine

    async def samples(
        self, turbine_id: str, start: datetime, end: datetime, readings: list[str]
    ) -> list[Any]:
        metrics = [metric for metric, reading in READINGS.items() if reading in readings]
        request = telemetry_pb2.StreamTurbineTelemetryRequest(
            turbine_id=turbine_id, from_time=_stamp(start), to_time=_stamp(end), metrics=metrics
        )
        points = await _call(
            self._clients["telemetry"], _collected(self._telemetry.StreamTurbineTelemetry),
            request, "Telemetry", STREAM_DEADLINE_SECONDS,
        )  # fmt: skip
        by_time: dict[datetime, dict[str, float | None]] = {}
        for point in points:
            at = point.event_time.ToDatetime(tzinfo=UTC)
            missing = point.quality == telemetry_pb2.QUALITY_MISSING
            by_time.setdefault(at, {})[READINGS.get(point.metric, "")] = (
                None if missing else point.value
            )
        return [engine.Sample(at, readings) for at, readings in sorted(by_time.items())]

    async def alarms(self, turbine_id: str) -> list[Any]:
        states = [alarm_pb2.ALARM_STATE_ACTIVE, alarm_pb2.ALARM_STATE_ACKNOWLEDGED,
                  alarm_pb2.ALARM_STATE_SHELVED, alarm_pb2.ALARM_STATE_CLEARED]  # fmt: skip
        request = alarm_pb2.ListAlarmsRequest(turbine_id=turbine_id, states=states, page_size=500)
        answer = await _call(self._clients["alarm"], self._alarm.ListAlarms, request, "Alarm")
        return [
            engine.AlarmView(
                a.code,
                a.tag,
                a.state != alarm_pb2.ALARM_STATE_CLEARED,
                a.raised_at.ToDatetime(tzinfo=UTC),
                a.cleared_at.ToDatetime(tzinfo=UTC) if a.HasField("cleared_at") else None,
            )
            for a in answer.alarms
        ]


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    return _stamp(value if value.tzinfo else value.replace(tzinfo=UTC))


def _version(row: Any) -> pb.FaultRuleVersion:
    return pb.FaultRuleVersion(
        rule_id=row.rule_id, version=row.version, kind=row.kind, definition_json=row.definition,
        content_hash=row.content_hash, created_by=row.created_by,
        created_at=_timestamp(row.created_at), reason=row.reason,
    )  # fmt: skip


def _definition(text: str) -> dict[str, Any]:
    if len(text) > MAXIMUM_DEFINITION:
        raise InvalidRequestError(f"definition_json is at most {MAXIMUM_DEFINITION} characters")
    try:
        value = json.loads(text)
    except ValueError as error:
        raise InvalidRequestError("definition_json is not JSON") from error
    if not isinstance(value, dict):
        raise InvalidRequestError("definition_json must be an object")
    return value


class FaultRuleService(fault_detection_pb2_grpc.FaultRuleServiceServicer):
    def __init__(
        self,
        engine_: AsyncEngine,
        authorizer: Authorizer,
        sources: Sources,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer, self._sources = engine_, authorizer, sources
        self._now = clock or (lambda: datetime.now(UTC))

    # --- who may do what

    async def _require_read(self) -> str:
        if not await self._authorizer.allowed(READ):
            raise PermissionDeniedError("You do not have permission to do this")
        return current_identity().tenant_id

    async def _require_manage(self) -> tuple[str, str]:
        """Only a person, with policy.manage and a recent second factor (FR-FLT-005, SEC-003)."""
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a person can change fault rules")
        if not await self._authorizer.allowed(MANAGE):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP_MESSAGE) from error
        return identity.tenant_id, identity.user_id

    # --- the rules

    async def _install_defaults(self, tenant: str) -> None:
        """A tenant's first look installs the default library, as version 1 of each, audited."""
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            any_rule = await connection.execute(
                select(FAULT_RULES.c.rule_id).where(FAULT_RULES.c.tenant_id == tenant).limit(1)
            )
            if any_rule.first() is not None:
                return
        now = self._now()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                for rule_id, name, description, definition in DEFAULT_RULES:
                    await self._write_version(
                        connection, tenant, rule_id, 1, definition, SYSTEM,
                        f"The default library, {LIBRARY}", now,
                    )  # fmt: skip
                    await connection.execute(
                        FAULT_RULES.insert().values(
                            tenant_id=tenant, rule_id=rule_id, name=name, description=description,
                            enabled=True, current_version=1, created_at=now, updated_at=now,
                        )
                    )  # fmt: skip
                    await audit(connection, tenant_id=tenant, actor_type=SYSTEM, actor_id=SYSTEM,
                                action="fault_rule.created", rule_id=rule_id)  # fmt: skip
        except IntegrityError:
            # Another call installed them first.
            return

    @staticmethod
    async def _write_version(
        connection: AsyncConnection, tenant: str, rule_id: str, version: int,
        definition: dict[str, Any], by: str, reason: str, now: datetime,
    ) -> None:  # fmt: skip
        await connection.execute(
            FAULT_RULE_VERSIONS.insert().values(
                tenant_id=tenant, rule_id=rule_id, version=version, kind=definition["kind"],
                definition=rules.canonical(definition),
                content_hash=rules.content_hash(rule_id, version, definition), created_by=by,
                created_at=now, reason=reason,
            )
        )  # fmt: skip

    async def _rules(self, connection: AsyncConnection, tenant: str) -> list[pb.FaultRule]:
        r, v = FAULT_RULES.c, FAULT_RULE_VERSIONS.c
        rows = (
            await connection.execute(
                select(FAULT_RULES, FAULT_RULE_VERSIONS)
                .join(
                    FAULT_RULE_VERSIONS,
                    (v.tenant_id == r.tenant_id)
                    & (v.rule_id == r.rule_id)
                    & (v.version == r.current_version),
                )
                .where(r.tenant_id == tenant)
                .order_by(r.rule_id)
            )
        ).all()
        return [
            pb.FaultRule(
                rule_id=row.rule_id,
                name=row.name,
                description=row.description,
                enabled=row.enabled,
                current=_version(row),
            )
            for row in rows
        ]

    async def _rule(self, connection: AsyncConnection, tenant: str, rule_id: str) -> pb.FaultRule:
        found = [rule for rule in await self._rules(connection, tenant) if rule.rule_id == rule_id]
        if not found:
            raise NotFoundError("No such fault rule")
        return found[0]

    async def ListFaultRules(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListFaultRulesRequest, context: Any
    ) -> pb.ListFaultRulesResponse:
        tenant = await self._require_read()
        await self._install_defaults(tenant)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            return pb.ListFaultRulesResponse(
                rules=await self._rules(connection, tenant), library=LIBRARY
            )

    async def GetFaultRule(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetFaultRuleRequest, context: Any
    ) -> pb.GetFaultRuleResponse:
        tenant = await self._require_read()
        await self._install_defaults(tenant)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rule = await self._rule(connection, tenant, request.rule_id)
            versions = (
                await connection.execute(
                    select(FAULT_RULE_VERSIONS)
                    .where(
                        FAULT_RULE_VERSIONS.c.tenant_id == tenant,
                        FAULT_RULE_VERSIONS.c.rule_id == request.rule_id,
                    )
                    .order_by(FAULT_RULE_VERSIONS.c.version)
                )
            ).all()
        return pb.GetFaultRuleResponse(rule=rule, versions=[_version(row) for row in versions])

    async def _change(
        self, method: str, request: Message, key: str,
        act: Callable[[AsyncConnection, str, str, datetime], Awaitable[pb.FaultRule]],
    ) -> Any:  # fmt: skip
        if len(key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant, user = await self._require_manage()
        await self._install_defaults(tenant)
        response_type = {
            "PublishFaultRuleVersion": pb.PublishFaultRuleVersionResponse,
            "SetFaultRuleEnabled": pb.SetFaultRuleEnabledResponse,
        }[method]
        now = self._now()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                try:
                    first = await remembered(connection, tenant, user, key, method, request)
                except KeyReusedError as error:
                    raise InvalidRequestError(str(error)) from error
                if first is not None:
                    return response_type.FromString(first)
                response = response_type(rule=await act(connection, tenant, user, now))
                await remember(
                    connection, tenant_id=tenant, user_id=user, key=key, method=method,
                    request=request, response=response, now=now,
                )  # fmt: skip
        except IntegrityError as error:
            raise ConflictError(
                "Someone else changed this rule first. Read it, and try again"
            ) from error
        return response

    async def PublishFaultRuleVersion(  # noqa: N802  # The name comes from the contract.
        self, request: pb.PublishFaultRuleVersionRequest, context: Any
    ) -> pb.PublishFaultRuleVersionResponse:
        if not _RULE_ID.fullmatch(request.rule_id):
            raise InvalidRequestError("rule_id is 3 to 64 lowercase letters, digits and hyphens")
        reason, name = request.reason.strip(), request.name.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")
        if len(name) > MAXIMUM_TEXT or len(request.description) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"name and description are at most {MAXIMUM_TEXT} characters")
        definition = _definition(request.definition_json)
        try:
            rules.parse(request.rule_id, 1, name, definition)
        except rules.RuleError as error:
            raise InvalidRequestError(f"The rule cannot be used: {error}") from error

        async def act(connection: AsyncConnection, tenant: str, user: str, now: datetime) -> Any:
            r = FAULT_RULES.c
            current = (
                await connection.execute(
                    select(FAULT_RULES).where(r.tenant_id == tenant, r.rule_id == request.rule_id)
                )
            ).one_or_none()
            have = current.current_version if current is not None else 0
            if have != request.expected_version:
                raise ConflictError(
                    f"This rule is at version {have}, not {request.expected_version}. Read it, "
                    "and try again"
                )
            version = have + 1
            await self._write_version(connection, tenant, request.rule_id, version, definition,
                                      user, reason, now)  # fmt: skip
            if current is None:
                if not name:
                    raise InvalidRequestError("A new rule needs a name")
                await connection.execute(
                    FAULT_RULES.insert().values(
                        tenant_id=tenant, rule_id=request.rule_id, name=name,
                        description=request.description.strip(), enabled=True,
                        current_version=version, created_at=now, updated_at=now,
                    )
                )  # fmt: skip
                action = "fault_rule.created"
            else:
                await connection.execute(
                    update(FAULT_RULES)
                    .where(r.tenant_id == tenant, r.rule_id == request.rule_id)
                    .values(
                        current_version=version, updated_at=now, name=name or current.name,
                        description=request.description.strip() or current.description,
                    )
                )  # fmt: skip
                action = "fault_rule.version_published"
            await audit(connection, tenant_id=tenant, actor_type="user", actor_id=user,
                        action=action, rule_id=request.rule_id)  # fmt: skip
            return await self._rule(connection, tenant, request.rule_id)

        response: pb.PublishFaultRuleVersionResponse = await self._change(
            "PublishFaultRuleVersion", request, request.idempotency_key, act
        )
        return response

    async def SetFaultRuleEnabled(  # noqa: N802  # The name comes from the contract.
        self, request: pb.SetFaultRuleEnabledRequest, context: Any
    ) -> pb.SetFaultRuleEnabledResponse:
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")

        async def act(connection: AsyncConnection, tenant: str, user: str, now: datetime) -> Any:
            rule = await self._rule(connection, tenant, request.rule_id)
            r = FAULT_RULES.c
            await connection.execute(
                update(FAULT_RULES)
                .where(r.tenant_id == tenant, r.rule_id == request.rule_id)
                .values(enabled=request.enabled, updated_at=now)
            )
            if rule.enabled != request.enabled:
                action = "fault_rule.enabled" if request.enabled else "fault_rule.disabled"
                await audit(connection, tenant_id=tenant, actor_type="user", actor_id=user,
                            action=action, rule_id=request.rule_id)  # fmt: skip
            return await self._rule(connection, tenant, request.rule_id)

        response: pb.SetFaultRuleEnabledResponse = await self._change(
            "SetFaultRuleEnabled", request, request.idempotency_key, act
        )
        return response

    # --- a dry run

    async def EvaluateTurbine(  # noqa: N802  # The name comes from the contract.
        self, request: pb.EvaluateTurbineRequest, context: Any
    ) -> pb.EvaluateTurbineResponse:
        tenant = await self._require_read()
        if not request.turbine_id:
            raise InvalidRequestError("turbine_id is required")
        await self._install_defaults(tenant)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            listed = [rule for rule in await self._rules(connection, tenant) if rule.enabled]
        enabled = [
            rules.parse(r.rule_id, r.current.version, r.name, json.loads(r.current.definition_json))
            for r in listed
        ]
        end = request.at.ToDatetime(tzinfo=UTC) if request.HasField("at") else self._now()
        start = end - engine.lookback(enabled)
        turbine = await self._sources.turbine(request.turbine_id)
        samples = await self._sources.samples(
            request.turbine_id, start, end, engine.readings_needed(enabled)
        )
        alarms = [a for a in await self._sources.alarms(request.turbine_id) if a.raised_at <= end]
        window = engine.Window(
            request.turbine_id, turbine.model.rated_power_kw, turbine.model.rotor_diameter_m,
            end, samples, alarms,
        )  # fmt: skip
        findings = engine.evaluate(enabled, window)
        return pb.EvaluateTurbineResponse(
            turbine_id=request.turbine_id, window_start=_stamp(start), window_end=_stamp(end),
            sample_count=len(samples), alarm_count=len(alarms),
            findings=[
                pb.Finding(
                    rule_id=f.rule_id, version=f.version, kind=f.kind, severity=f.severity,
                    amount=f.amount, summary=f.summary,
                    observed_json=json.dumps(dict(f.observed), sort_keys=True),
                    since=_timestamp(f.since),
                )
                for f in findings
            ],
        )  # fmt: skip
