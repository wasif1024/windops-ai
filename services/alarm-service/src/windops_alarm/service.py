"""The gRPC API. It asks Authorization first, and every query stays in the caller's tenant.

A turbine's wind farm comes from the Asset Service, asked as the caller, because a grant can be
for a wind farm (RBAC-002). A caller who may read alarms across the whole tenant needs no lookup.
"""

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.alarm.v1 import alarm_pb2, alarm_pb2_grpc
from windops.asset.v1 import asset_pb2
from windops.authorization.v1 import authorization_pb2
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

from windops_alarm import catalog
from windops_alarm.lifecycle import USER, change, load
from windops_alarm.store import (
    ACKNOWLEDGED,
    ACTIVE,
    ALARM_TRANSITIONS,
    ALARMS,
    CLEARED,
    SHELVED,
    KeyReusedError,
    remember,
    remembered,
)

READ = "alarm.read"
ACKNOWLEDGE = "alarm.acknowledge"
DEADLINE_SECONDS = 3.0
DEFAULT_PAGE, MAXIMUM_PAGE = 100, 500
MAXIMUM_KEY = 64
MAXIMUM_NOTE = 500
MAXIMUM_SHELVE_MINUTES = 24 * 60
TURBINE_PAGE = 500
Response = TypeVar("Response", bound=Message)

STATES = {
    alarm_pb2.ALARM_STATE_ACTIVE: ACTIVE,
    alarm_pb2.ALARM_STATE_ACKNOWLEDGED: ACKNOWLEDGED,
    alarm_pb2.ALARM_STATE_CLEARED: CLEARED,
    alarm_pb2.ALARM_STATE_SHELVED: SHELVED,
}
STATE_OF = {name: number for number, name in STATES.items()}
SEVERITY_OF = {
    "low": alarm_pb2.ALARM_SEVERITY_LOW,
    "medium": alarm_pb2.ALARM_SEVERITY_MEDIUM,
    "high": alarm_pb2.ALARM_SEVERITY_HIGH,
    "critical": alarm_pb2.ALARM_SEVERITY_CRITICAL,
}
# Most severe first, in SQL as well, so a page break never splits the order.
_RANK = case({"critical": 4, "high": 3, "medium": 2, "low": 1}, value=ALARMS.c.severity, else_=0)


@dataclass(frozen=True)
class Readable:
    """Where the caller may read: everything, or only some wind farms and turbines."""

    everything: bool
    wind_farms: frozenset[str]
    turbines: frozenset[str]


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...
    async def readable(self, permission: str) -> Readable: ...


class Assets(Protocol):
    async def wind_farm_of(self, turbine_id: str) -> str: ...
    async def turbines_in(self, wind_farm_id: str) -> list[str]: ...


async def _call(client: ServiceClient, method: Any, request: Message, who: str) -> Any:
    try:
        return await client.call(
            method, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
        )
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError("No such alarm") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        try:
            answer = await _call(self._client, self._stub.Check, request, "Authorization")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)

    async def readable(self, permission: str) -> Readable:
        request = authorization_pb2.ListMyPermissionsRequest()
        try:
            answer = await _call(self._client, self._stub.ListMyPermissions, request, "Auth")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        scopes = [item.scope for item in answer.permissions if item.permission == permission]
        return Readable(
            everything=any(not s.wind_farm_id and not s.turbine_id for s in scopes),
            wind_farms=frozenset(
                s.wind_farm_id for s in scopes if s.wind_farm_id and not s.turbine_id
            ),
            turbines=frozenset(s.turbine_id for s in scopes if s.turbine_id),
        )


class AssetClient:
    """Asks the Asset Service, as the caller, which wind farm holds a turbine."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def wind_farm_of(self, turbine_id: str) -> str:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        answer = await _call(self._client, self._stub.GetTurbine, request, "The Asset Service")
        return str(answer.turbine.wind_farm_id)

    async def turbines_in(self, wind_farm_id: str) -> list[str]:
        found: list[str] = []
        token = ""
        while True:
            request = asset_pb2.ListTurbinesRequest(
                wind_farm_id=wind_farm_id, page_size=TURBINE_PAGE, page_token=token
            )
            page = await _call(self._client, self._stub.ListTurbines, request, "Asset")
            found += [turbine.turbine_id for turbine in page.turbines]
            token = page.next_page_token
            if not token:
                return found


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def to_message(row: Any) -> alarm_pb2.Alarm:
    return alarm_pb2.Alarm(
        alarm_id=row.alarm_id, turbine_id=row.turbine_id, code=row.code, tag=row.tag,
        description=row.description,
        severity=SEVERITY_OF.get(row.severity, alarm_pb2.ALARM_SEVERITY_UNSPECIFIED),
        severity_source=row.severity_source, state=STATE_OF[row.state], source=row.source,
        raised_at=_timestamp(row.raised_at), received_at=_timestamp(row.received_at),
        acknowledged_at=_timestamp(row.acknowledged_at), acknowledged_by=row.acknowledged_by,
        cleared_at=_timestamp(row.cleared_at), shelved_until=_timestamp(row.shelved_until),
        shelved_by=row.shelved_by, shelve_reason=row.shelve_reason,
        repeat_count=row.repeat_count, version=row.version,
    )  # fmt: skip


def _page_token(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _offset(token: str) -> int:
    if not token:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(token.encode()).decode()))
    except (ValueError, UnicodeDecodeError) as error:
        raise InvalidRequestError("page_token is not one this service gave") from error


class AlarmService(alarm_pb2_grpc.AlarmServiceServicer):
    def __init__(
        self,
        engine: AsyncEngine,
        authorizer: Authorizer,
        assets: Assets,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer, self._assets = engine, authorizer, assets
        self._now = clock or (lambda: datetime.now(UTC))

    # --- who may do what

    async def _allowed(self, permission: str, turbine_id: str) -> bool:
        """For this turbine, its wind farm or the whole tenant."""
        if await self._authorizer.allowed(permission):
            return True
        wind_farm = await self._assets.wind_farm_of(turbine_id)
        return await self._authorizer.allowed(permission, wind_farm, turbine_id)

    async def _require(self, permission: str, turbine_id: str) -> None:
        """An alarm the caller may not read is not found. One they may read, but not act on,
        is refused."""
        if not await self._allowed(READ, turbine_id):
            raise NotFoundError("No such alarm")
        if permission != READ and not await self._allowed(permission, turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")

    async def _turbines_in(self, wind_farm_id: str) -> set[str]:
        """A wind farm's turbines, as the caller sees them: none, if they cannot see it."""
        try:
            return set(await self._assets.turbines_in(wind_farm_id))
        except NotFoundError:
            return set()

    async def _readable_turbines(self, wind_farm_id: str) -> frozenset[str] | None:
        """The turbines the caller may read alarms for, or None for every turbine."""
        readable = await self._authorizer.readable(READ)
        if readable.everything:
            return frozenset(await self._turbines_in(wind_farm_id)) if wind_farm_id else None
        turbines = set(readable.turbines)
        for farm in readable.wind_farms:
            turbines |= await self._turbines_in(farm)
        if wind_farm_id:
            turbines &= await self._turbines_in(wind_farm_id)
        return frozenset(turbines)

    # --- reads

    async def ListAlarms(  # noqa: N802  # The name comes from the contract.
        self, request: alarm_pb2.ListAlarmsRequest, context: Any
    ) -> alarm_pb2.ListAlarmsResponse:
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        unknown = [s for s in request.states if s not in STATES]
        if unknown:
            raise InvalidRequestError("states holds an unknown state")
        offset = _offset(request.page_token)
        turbines = await self._readable_turbines(request.wind_farm_id)
        tenant = current_identity().tenant_id
        query = select(ALARMS).where(ALARMS.c.tenant_id == tenant)
        states = [STATES[s] for s in request.states] or [ACTIVE, ACKNOWLEDGED, SHELVED]
        query = query.where(ALARMS.c.state.in_(states))
        if request.turbine_id:
            query = query.where(ALARMS.c.turbine_id == request.turbine_id)
        if turbines is not None:
            if not turbines:
                return alarm_pb2.ListAlarmsResponse()
            query = query.where(ALARMS.c.turbine_id.in_(sorted(turbines)))
        query = (
            query.order_by(_RANK.desc(), ALARMS.c.raised_at.desc(), ALARMS.c.alarm_id)
            .offset(offset)
            .limit(size + 1)
        )
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(query)).all()
        more = len(rows) > size
        return alarm_pb2.ListAlarmsResponse(
            alarms=[to_message(row) for row in rows[:size]],
            next_page_token=_page_token(offset + size) if more else "",
        )

    async def _found(self, connection: AsyncConnection, tenant: str, alarm_id: str) -> Any:
        if not alarm_id:
            raise InvalidRequestError("alarm_id is required")
        row = await load(connection, tenant, alarm_id)
        if row is None:
            raise NotFoundError("No such alarm")
        return row

    async def GetAlarm(  # noqa: N802  # The name comes from the contract.
        self, request: alarm_pb2.GetAlarmRequest, context: Any
    ) -> alarm_pb2.GetAlarmResponse:
        tenant = current_identity().tenant_id
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            alarm = await self._found(connection, tenant, request.alarm_id)
            await self._require(READ, alarm.turbine_id)
            history = (
                await connection.execute(
                    select(ALARM_TRANSITIONS)
                    .where(
                        ALARM_TRANSITIONS.c.tenant_id == tenant,
                        ALARM_TRANSITIONS.c.alarm_id == alarm.alarm_id,
                    )
                    .order_by(ALARM_TRANSITIONS.c.version)
                )
            ).all()
        return alarm_pb2.GetAlarmResponse(
            alarm=to_message(alarm),
            transitions=[
                alarm_pb2.AlarmTransition(
                    at=_timestamp(t.at),
                    from_state=STATE_OF.get(t.from_state, alarm_pb2.ALARM_STATE_UNSPECIFIED),
                    to_state=STATE_OF[t.to_state],
                    actor_type=t.actor_type,
                    actor_id=t.actor_id,
                    note=t.note,
                )
                for t in history
            ],
        )

    async def GetAlarmCatalog(  # noqa: N802  # The name comes from the contract.
        self, request: alarm_pb2.GetAlarmCatalogRequest, context: Any
    ) -> alarm_pb2.GetAlarmCatalogResponse:
        # Anyone who may read some alarms may read what the codes mean.
        readable = await self._authorizer.readable(READ)
        if not (readable.everything or readable.wind_farms or readable.turbines):
            raise PermissionDeniedError("You do not have permission to do this")
        return alarm_pb2.GetAlarmCatalogResponse(
            version=catalog.VERSION,
            entries=[
                alarm_pb2.CatalogEntry(
                    code=entry.code,
                    description=entry.description,
                    default_severity=SEVERITY_OF[entry.severity],
                    component=entry.component,
                )
                for entry in catalog.ENTRIES
            ],
        )

    # --- what operators do

    async def _act(
        self,
        method: str,
        request: Message,
        response_type: type[Response],
        alarm_id: str,
        key: str,
        act: Callable[[AsyncConnection, Any, str, datetime], Awaitable[Any]],
    ) -> Response:
        """One operator action: checked, applied once, kept in history, announced and audited."""
        if len(key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a user can act on an alarm")
        tenant, user = identity.tenant_id, identity.user_id
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, user, key, method, request)
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return response_type.FromString(first)
            alarm = await self._found(connection, tenant, alarm_id)
            await self._require(ACKNOWLEDGE, alarm.turbine_id)
            changed = await act(connection, alarm, user, now)
            response = response_type(alarm=to_message(changed))
            await remember(
                connection, tenant_id=tenant, user_id=user, key=key, method=method,
                request=request, response=response, now=now,
            )  # fmt: skip
        return response

    async def AcknowledgeAlarm(  # noqa: N802  # The name comes from the contract.
        self, request: alarm_pb2.AcknowledgeAlarmRequest, context: Any
    ) -> alarm_pb2.AcknowledgeAlarmResponse:
        comment = request.comment.strip()
        if len(comment) > MAXIMUM_NOTE:
            raise InvalidRequestError(f"comment is at most {MAXIMUM_NOTE} characters")

        async def act(connection: AsyncConnection, alarm: Any, user: str, now: datetime) -> Any:
            if alarm.state != ACTIVE:
                raise ConflictError(
                    f"Only an active alarm can be acknowledged; it is {alarm.state}"
                )
            return await change(
                connection, alarm, at=now, now=now, actor_type=USER, actor_id=user,
                note=comment, audit_action="alarm.acknowledged", state=ACKNOWLEDGED,
                acknowledged_at=now, acknowledged_by=user,
            )  # fmt: skip

        return await self._act(
            "AcknowledgeAlarm", request, alarm_pb2.AcknowledgeAlarmResponse, request.alarm_id,
            request.idempotency_key, act,
        )  # fmt: skip

    async def ShelveAlarm(  # noqa: N802  # The name comes from the contract.
        self, request: alarm_pb2.ShelveAlarmRequest, context: Any
    ) -> alarm_pb2.ShelveAlarmResponse:
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_NOTE:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_NOTE} characters")
        if not 1 <= request.minutes <= MAXIMUM_SHELVE_MINUTES:
            raise InvalidRequestError(f"minutes is from 1 to {MAXIMUM_SHELVE_MINUTES}")

        async def act(connection: AsyncConnection, alarm: Any, user: str, now: datetime) -> Any:
            if alarm.state not in (ACTIVE, ACKNOWLEDGED):
                raise ConflictError(f"Only a standing alarm can be shelved; it is {alarm.state}")
            return await change(
                connection, alarm, at=now, now=now, actor_type=USER, actor_id=user,
                note=reason, audit_action="alarm.shelved", state=SHELVED,
                shelved_until=now + timedelta(minutes=request.minutes), shelved_by=user,
                shelve_reason=reason, shelved_from=alarm.state,
            )  # fmt: skip

        return await self._act(
            "ShelveAlarm", request, alarm_pb2.ShelveAlarmResponse, request.alarm_id,
            request.idempotency_key, act,
        )  # fmt: skip

    async def UnshelveAlarm(  # noqa: N802  # The name comes from the contract.
        self, request: alarm_pb2.UnshelveAlarmRequest, context: Any
    ) -> alarm_pb2.UnshelveAlarmResponse:
        async def act(connection: AsyncConnection, alarm: Any, user: str, now: datetime) -> Any:
            if alarm.state != SHELVED:
                raise ConflictError(f"Only a shelved alarm can be unshelved; it is {alarm.state}")
            return await change(
                connection, alarm, at=now, now=now, actor_type=USER, actor_id=user,
                audit_action="alarm.unshelved", state=alarm.shelved_from or ACTIVE,
                shelved_until=None, shelved_by="", shelve_reason="", shelved_from="",
            )  # fmt: skip

        return await self._act(
            "UnshelveAlarm", request, alarm_pb2.UnshelveAlarmResponse, request.alarm_id,
            request.idempotency_key, act,
        )  # fmt: skip
