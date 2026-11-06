"""The gRPC API. It asks Authorization first, and every query stays in the caller's tenant."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2
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

from windops_asset import rules
from windops_asset.rules import AssetRuleError
from windops_asset.store import (
    COMPONENTS,
    TURBINES,
    WIND_FARMS,
    KeyReusedError,
    record_change,
    remember,
    remembered,
)

READ = "turbine.read"
MANAGE = "asset.manage"
DEADLINE_SECONDS = 3.0
DEFAULT_PAGE, MAXIMUM_PAGE = 100, 500
MAXIMUM_KEY = 64
Response = TypeVar("Response", bound=Message)


@dataclass(frozen=True)
class Readable:
    """Where the caller may read: everything, or only some wind farms and turbines (RBAC-002)."""

    everything: bool
    wind_farms: frozenset[str]
    turbines: frozenset[str]

    def covers(self, wind_farm_id: str, turbine_id: str = "") -> bool:
        return (
            self.everything
            or wind_farm_id in self.wind_farms
            or (bool(turbine_id) and turbine_id in self.turbines)
        )

    def sees_farm(self, wind_farm_id: str, farms_of_turbines: frozenset[str]) -> bool:
        return (
            self.everything or wind_farm_id in self.wind_farms or wind_farm_id in farms_of_turbines
        )


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...
    async def readable(self) -> Readable: ...


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def _call(self, method: Callable[..., Any], request: Message) -> Any:
        try:
            return await self._client.call(
                method, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("Permissions cannot be checked right now") from error

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        return bool((await self._call(self._stub.Check, request)).allowed)

    async def readable(self) -> Readable:
        answer = await self._call(
            self._stub.ListMyPermissions, authorization_pb2.ListMyPermissionsRequest()
        )
        scopes = [item.scope for item in answer.permissions if item.permission == READ]
        return Readable(
            everything=any(not s.wind_farm_id and not s.turbine_id for s in scopes),
            wind_farms=frozenset(
                s.wind_farm_id for s in scopes if s.wind_farm_id and not s.turbine_id
            ),
            turbines=frozenset(s.turbine_id for s in scopes if s.turbine_id),
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


def _wind_farm(row: Any) -> asset_pb2.WindFarm:
    return asset_pb2.WindFarm(
        wind_farm_id=row.wind_farm_id,
        name=row.name,
        country=row.country,
        location=asset_pb2.Location(latitude=row.latitude, longitude=row.longitude),
        time_zone=row.time_zone,
        offshore=row.offshore,
        version=row.version,
        created_at=_timestamp(row.created_at),
        updated_at=_timestamp(row.updated_at),
    )


def _turbine(row: Any) -> asset_pb2.Turbine:
    return asset_pb2.Turbine(
        turbine_id=row.turbine_id,
        wind_farm_id=row.wind_farm_id,
        name=row.name,
        location=asset_pb2.Location(latitude=row.latitude, longitude=row.longitude),
        model=asset_pb2.TurbineModel(
            manufacturer=row.manufacturer,
            model=row.model,
            rated_power_kw=row.rated_power_kw,
            rotor_diameter_m=row.rotor_diameter_m,
            hub_height_m=row.hub_height_m,
        ),
        serial_number=row.serial_number,
        commissioned_on=row.commissioned_on,
        status=row.status,
        status_reason=row.status_reason,
        status_changed_at=_timestamp(row.status_changed_at),
        version=row.version,
        created_at=_timestamp(row.created_at),
        updated_at=_timestamp(row.updated_at),
    )


def _component(row: Any) -> asset_pb2.Component:
    return asset_pb2.Component(
        component_id=row.component_id,
        turbine_id=row.turbine_id,
        parent_component_id=row.parent_component_id,
        kind=row.kind,
        name=row.name,
        manufacturer=row.manufacturer,
        model=row.model,
        serial_number=row.serial_number,
        installed_on=row.installed_on,
        status=row.status,
        status_reason=row.status_reason,
        version=row.version,
        updated_at=_timestamp(row.updated_at),
    )


def _checked(check: Callable[[], None]) -> None:
    try:
        check()
    except AssetRuleError as error:
        raise InvalidRequestError(str(error)) from error


def _farm_fields(request: Any) -> dict[str, Any]:
    def check() -> None:
        rules.check_id("wind_farm_id", request.wind_farm_id)
        rules.check_text("name", request.name, required=True)
        rules.check_country(request.country)
        rules.check_location(request.location.latitude, request.location.longitude)
        rules.check_time_zone(request.time_zone)

    _checked(check)
    return {
        "name": request.name.strip(),
        "country": request.country,
        "latitude": request.location.latitude,
        "longitude": request.location.longitude,
        "time_zone": request.time_zone,
        "offshore": request.offshore,
    }


def _turbine_fields(request: Any) -> dict[str, Any]:
    model = request.model

    def check() -> None:
        rules.check_id("turbine_id", request.turbine_id)
        rules.check_text("name", request.name, required=True)
        rules.check_location(request.location.latitude, request.location.longitude)
        for name in ("manufacturer", "model"):
            rules.check_text(name, getattr(model, name), required=True)
        rules.check_model(model.rated_power_kw, model.rotor_diameter_m, model.hub_height_m)
        rules.check_text("serial_number", request.serial_number)
        rules.check_date("commissioned_on", request.commissioned_on)

    _checked(check)
    return {
        "name": request.name.strip(),
        "latitude": request.location.latitude,
        "longitude": request.location.longitude,
        "manufacturer": model.manufacturer.strip(),
        "model": model.model.strip(),
        "rated_power_kw": model.rated_power_kw,
        "rotor_diameter_m": model.rotor_diameter_m,
        "hub_height_m": model.hub_height_m,
        "serial_number": request.serial_number.strip(),
        "commissioned_on": request.commissioned_on,
    }


class AssetService(asset_pb2_grpc.AssetServiceServicer):
    def __init__(self, engine: AsyncEngine, authorizer: Authorizer) -> None:
        self._engine = engine
        self._authorizer = authorizer

    # --- who may do what

    async def _require_read(self, wind_farm_id: str, turbine_id: str = "") -> None:
        if not await self._authorizer.allowed(READ, wind_farm_id, turbine_id):
            raise PermissionDeniedError("You do not have permission to read this")

    async def _require_change(self, wind_farm_id: str = "", turbine_id: str = "") -> str:
        """Return the user making a change, or refuse (SEC-003)."""
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a user can change assets")
        if not await self._authorizer.allowed(MANAGE, wind_farm_id, turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP_MESSAGE) from error
        return identity.user_id

    @staticmethod
    def _tenant() -> str:
        return current_identity().tenant_id

    @staticmethod
    def _check_key(key: str) -> None:
        if len(key) > MAXIMUM_KEY:
            raise InvalidRequestError("idempotency_key is too long")

    async def _replay(
        self,
        connection: AsyncConnection,
        user: str,
        method: str,
        request: Any,
        response_type: type[Response],
    ) -> Response | None:
        try:
            stored = await remembered(
                connection, self._tenant(), user, request.idempotency_key, method, request
            )
        except KeyReusedError as error:
            raise ConflictError(str(error)) from error
        return None if stored is None else response_type.FromString(stored)

    async def _change(
        self,
        method: str,
        request: Any,
        response_type: type[Response],
        user: str,
        work: Callable[[AsyncConnection, datetime], Awaitable[Response]],
    ) -> Response:
        """Run a change in one transaction, with its replay check and its remembered answer."""
        self._check_key(request.idempotency_key)
        tenant = self._tenant()
        now = _now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            replay = await self._replay(connection, user, method, request, response_type)
            if replay is not None:
                return replay
            response = await work(connection, now)
            await remember(
                connection,
                tenant_id=tenant,
                user_id=user,
                key=request.idempotency_key,
                method=method,
                request=request,
                response=response,
                now=now,
            )
        return response

    # --- reading

    async def _row(self, connection: AsyncConnection, query: Any, what: str) -> Any:
        row = (await connection.execute(query)).one_or_none()
        if row is None:
            raise NotFoundError(f"No such {what}")
        return row

    async def _turbine_row(self, connection: AsyncConnection, turbine_id: str) -> Any:
        return await self._row(
            connection,
            select(TURBINES).where(
                TURBINES.c.tenant_id == self._tenant(), TURBINES.c.turbine_id == turbine_id
            ),
            "turbine",
        )

    async def ListWindFarms(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.ListWindFarmsRequest, context: Any
    ) -> asset_pb2.ListWindFarmsResponse:
        readable = await self._authorizer.readable()
        tenant = self._tenant()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            farms = (
                await connection.execute(
                    select(WIND_FARMS)
                    .where(WIND_FARMS.c.tenant_id == tenant)
                    .order_by(WIND_FARMS.c.wind_farm_id)
                )
            ).all()
            # A turbine grant shows its wind farm too, so the turbine can be found.
            farms_of_turbines: frozenset[str] = frozenset()
            if readable.turbines:
                farms_of_turbines = frozenset(
                    (
                        await connection.execute(
                            select(TURBINES.c.wind_farm_id).where(
                                TURBINES.c.tenant_id == tenant,
                                TURBINES.c.turbine_id.in_(readable.turbines),
                            )
                        )
                    ).scalars()
                )
        return asset_pb2.ListWindFarmsResponse(
            wind_farms=[
                _wind_farm(row)
                for row in farms
                if readable.sees_farm(row.wind_farm_id, farms_of_turbines)
            ]
        )

    async def GetWindFarm(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.GetWindFarmRequest, context: Any
    ) -> asset_pb2.GetWindFarmResponse:
        await self._require_read(request.wind_farm_id)
        tenant = self._tenant()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._row(
                connection,
                select(WIND_FARMS).where(
                    WIND_FARMS.c.tenant_id == tenant,
                    WIND_FARMS.c.wind_farm_id == request.wind_farm_id,
                ),
                "wind farm",
            )
        return asset_pb2.GetWindFarmResponse(wind_farm=_wind_farm(row))

    async def ListTurbines(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.ListTurbinesRequest, context: Any
    ) -> asset_pb2.ListTurbinesResponse:
        if request.page_size < 0:
            raise InvalidRequestError("page_size cannot be negative")
        limit = min(request.page_size or DEFAULT_PAGE, MAXIMUM_PAGE)
        readable = await self._authorizer.readable()
        tenant = self._tenant()
        query = select(TURBINES).where(
            TURBINES.c.tenant_id == tenant, TURBINES.c.turbine_id > request.page_token
        )
        if request.wind_farm_id:
            query = query.where(TURBINES.c.wind_farm_id == request.wind_farm_id)
        if not readable.everything:
            query = query.where(
                TURBINES.c.wind_farm_id.in_(readable.wind_farms)
                | TURBINES.c.turbine_id.in_(readable.turbines)
            )
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            rows = (
                await connection.execute(query.order_by(TURBINES.c.turbine_id).limit(limit))
            ).all()
        next_token = rows[-1].turbine_id if len(rows) == limit else ""
        return asset_pb2.ListTurbinesResponse(
            turbines=[_turbine(row) for row in rows], next_page_token=next_token
        )

    async def GetTurbine(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.GetTurbineRequest, context: Any
    ) -> asset_pb2.GetTurbineResponse:
        tenant = self._tenant()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            turbine = await self._turbine_row(connection, request.turbine_id)
            components = (
                await connection.execute(
                    select(COMPONENTS)
                    .where(
                        COMPONENTS.c.tenant_id == tenant,
                        COMPONENTS.c.turbine_id == request.turbine_id,
                    )
                    .order_by(COMPONENTS.c.kind, COMPONENTS.c.name)
                )
            ).all()
        await self._require_read(turbine.wind_farm_id, turbine.turbine_id)
        return asset_pb2.GetTurbineResponse(
            turbine=_turbine(turbine), components=[_component(row) for row in components]
        )

    # --- wind farms

    async def CreateWindFarm(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.CreateWindFarmRequest, context: Any
    ) -> asset_pb2.CreateWindFarmResponse:
        fields = _farm_fields(request)
        user = await self._require_change(request.wind_farm_id)
        tenant = self._tenant()

        async def work(
            connection: AsyncConnection, now: datetime
        ) -> asset_pb2.CreateWindFarmResponse:
            existing = (
                await connection.execute(
                    select(WIND_FARMS).where(
                        WIND_FARMS.c.tenant_id == tenant,
                        WIND_FARMS.c.wind_farm_id == request.wind_farm_id,
                    )
                )
            ).one_or_none()
            if existing is not None:
                if {name: getattr(existing, name) for name in fields} != fields:
                    raise ConflictError("A wind farm with this ID already exists")
                return asset_pb2.CreateWindFarmResponse(wind_farm=_wind_farm(existing))
            row = (
                await connection.execute(
                    WIND_FARMS.insert()
                    .values(
                        tenant_id=tenant,
                        wind_farm_id=request.wind_farm_id,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        **fields,
                    )
                    .returning(*WIND_FARMS.c)
                )
            ).one()
            await record_change(
                connection,
                tenant_id=tenant,
                actor_id=user,
                action="asset.wind_farm.created",
                resource_type="wind_farm",
                resource_id=request.wind_farm_id,
            )
            return asset_pb2.CreateWindFarmResponse(wind_farm=_wind_farm(row), created=True)

        return await self._change(
            "CreateWindFarm", request, asset_pb2.CreateWindFarmResponse, user, work
        )

    async def UpdateWindFarm(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.UpdateWindFarmRequest, context: Any
    ) -> asset_pb2.UpdateWindFarmResponse:
        fields = _farm_fields(request)
        user = await self._require_change(request.wind_farm_id)
        tenant = self._tenant()

        async def work(
            connection: AsyncConnection, now: datetime
        ) -> asset_pb2.UpdateWindFarmResponse:
            row = (
                await connection.execute(
                    update(WIND_FARMS)
                    .where(
                        WIND_FARMS.c.tenant_id == tenant,
                        WIND_FARMS.c.wind_farm_id == request.wind_farm_id,
                    )
                    .values(version=WIND_FARMS.c.version + 1, updated_at=now, **fields)
                    .returning(*WIND_FARMS.c)
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("No such wind farm")
            await record_change(
                connection,
                tenant_id=tenant,
                actor_id=user,
                action="asset.wind_farm.updated",
                resource_type="wind_farm",
                resource_id=request.wind_farm_id,
            )
            return asset_pb2.UpdateWindFarmResponse(wind_farm=_wind_farm(row))

        return await self._change(
            "UpdateWindFarm", request, asset_pb2.UpdateWindFarmResponse, user, work
        )

    # --- turbines

    async def CreateTurbine(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.CreateTurbineRequest, context: Any
    ) -> asset_pb2.CreateTurbineResponse:
        fields = _turbine_fields(request)
        _checked(lambda: rules.check_id("wind_farm_id", request.wind_farm_id))
        user = await self._require_change(request.wind_farm_id)
        tenant = self._tenant()

        async def work(
            connection: AsyncConnection, now: datetime
        ) -> asset_pb2.CreateTurbineResponse:
            await self._row(
                connection,
                select(WIND_FARMS.c.wind_farm_id).where(
                    WIND_FARMS.c.tenant_id == tenant,
                    WIND_FARMS.c.wind_farm_id == request.wind_farm_id,
                ),
                "wind farm",
            )
            existing = (
                await connection.execute(
                    select(TURBINES).where(
                        TURBINES.c.tenant_id == tenant, TURBINES.c.turbine_id == request.turbine_id
                    )
                )
            ).one_or_none()
            if existing is not None:
                same = existing.wind_farm_id == request.wind_farm_id and all(
                    getattr(existing, name) == value for name, value in fields.items()
                )
                if not same:
                    raise ConflictError("A turbine with this ID already exists")
                return asset_pb2.CreateTurbineResponse(turbine=_turbine(existing))
            row = (
                await connection.execute(
                    TURBINES.insert()
                    .values(
                        tenant_id=tenant,
                        turbine_id=request.turbine_id,
                        wind_farm_id=request.wind_farm_id,
                        status="operating",
                        status_reason="",
                        status_changed_at=now,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        **fields,
                    )
                    .returning(*TURBINES.c)
                )
            ).one()
            await record_change(
                connection,
                tenant_id=tenant,
                actor_id=user,
                action="asset.turbine.created",
                resource_type="turbine",
                resource_id=request.turbine_id,
                turbine=row,
            )
            return asset_pb2.CreateTurbineResponse(turbine=_turbine(row), created=True)

        return await self._change(
            "CreateTurbine", request, asset_pb2.CreateTurbineResponse, user, work
        )

    async def _scope_of(self, turbine_id: str) -> str:
        """The wind farm a turbine is in, so a scoped grant can be checked before a change."""
        tenant = self._tenant()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._turbine_row(connection, turbine_id)
        return str(row.wind_farm_id)

    async def _bump_turbine(
        self, connection: AsyncConnection, turbine_id: str, now: datetime, **values: Any
    ) -> Any:
        row = (
            await connection.execute(
                update(TURBINES)
                .where(TURBINES.c.tenant_id == self._tenant(), TURBINES.c.turbine_id == turbine_id)
                .values(version=TURBINES.c.version + 1, updated_at=now, **values)
                .returning(*TURBINES.c)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("No such turbine")
        return row

    async def UpdateTurbine(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.UpdateTurbineRequest, context: Any
    ) -> asset_pb2.UpdateTurbineResponse:
        fields = _turbine_fields(request)
        user = await self._require_change(
            await self._scope_of(request.turbine_id), request.turbine_id
        )
        tenant = self._tenant()

        async def work(
            connection: AsyncConnection, now: datetime
        ) -> asset_pb2.UpdateTurbineResponse:
            row = await self._bump_turbine(connection, request.turbine_id, now, **fields)
            await record_change(
                connection,
                tenant_id=tenant,
                actor_id=user,
                action="asset.turbine.updated",
                resource_type="turbine",
                resource_id=request.turbine_id,
                turbine=row,
            )
            return asset_pb2.UpdateTurbineResponse(turbine=_turbine(row))

        return await self._change(
            "UpdateTurbine", request, asset_pb2.UpdateTurbineResponse, user, work
        )

    async def SetTurbineStatus(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.SetTurbineStatusRequest, context: Any
    ) -> asset_pb2.SetTurbineStatusResponse:
        _checked(lambda: rules.check_status("turbine", request.status, request.reason))
        user = await self._require_change(
            await self._scope_of(request.turbine_id), request.turbine_id
        )
        tenant = self._tenant()

        async def work(
            connection: AsyncConnection, now: datetime
        ) -> asset_pb2.SetTurbineStatusResponse:
            row = await self._bump_turbine(
                connection,
                request.turbine_id,
                now,
                status=request.status,
                status_reason=request.reason.strip(),
                status_changed_at=now,
            )
            await record_change(
                connection,
                tenant_id=tenant,
                actor_id=user,
                action=f"asset.turbine.status.{request.status}",
                resource_type="turbine",
                resource_id=request.turbine_id,
                turbine=row,
            )
            return asset_pb2.SetTurbineStatusResponse(turbine=_turbine(row))

        return await self._change(
            "SetTurbineStatus", request, asset_pb2.SetTurbineStatusResponse, user, work
        )

    # --- components

    async def AddComponent(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.AddComponentRequest, context: Any
    ) -> asset_pb2.AddComponentResponse:
        def check() -> None:
            rules.check_component_kind(request.kind)
            rules.check_text("name", request.name, required=True)
            for name in ("manufacturer", "model", "serial_number"):
                rules.check_text(name, getattr(request, name))
            rules.check_date("installed_on", request.installed_on)

        _checked(check)
        user = await self._require_change(
            await self._scope_of(request.turbine_id), request.turbine_id
        )
        tenant = self._tenant()

        async def work(
            connection: AsyncConnection, now: datetime
        ) -> asset_pb2.AddComponentResponse:
            if request.parent_component_id:
                parent = await self._row(
                    connection,
                    select(COMPONENTS.c.turbine_id).where(
                        COMPONENTS.c.tenant_id == tenant,
                        COMPONENTS.c.component_id == request.parent_component_id,
                    ),
                    "parent component",
                )
                # A part belongs under a part of the same turbine. Parents come first, so no cycle.
                if parent.turbine_id != request.turbine_id:
                    raise InvalidRequestError("The parent component is on another turbine")
            row = (
                await connection.execute(
                    COMPONENTS.insert()
                    .values(
                        tenant_id=tenant,
                        component_id=uuid.uuid4().hex,
                        turbine_id=request.turbine_id,
                        parent_component_id=request.parent_component_id,
                        kind=request.kind,
                        name=request.name.strip(),
                        manufacturer=request.manufacturer.strip(),
                        model=request.model.strip(),
                        serial_number=request.serial_number.strip(),
                        installed_on=request.installed_on,
                        status="in_service",
                        status_reason="",
                        version=1,
                        updated_at=now,
                    )
                    .returning(*COMPONENTS.c)
                )
            ).one()
            turbine = await self._bump_turbine(connection, request.turbine_id, now)
            await record_change(
                connection,
                tenant_id=tenant,
                actor_id=user,
                action=f"asset.component.added.{request.kind}",
                resource_type="component",
                resource_id=row.component_id,
                turbine=turbine,
            )
            return asset_pb2.AddComponentResponse(component=_component(row), created=True)

        return await self._change(
            "AddComponent", request, asset_pb2.AddComponentResponse, user, work
        )

    async def SetComponentStatus(  # noqa: N802  # The name comes from the contract.
        self, request: asset_pb2.SetComponentStatusRequest, context: Any
    ) -> asset_pb2.SetComponentStatusResponse:
        _checked(lambda: rules.check_status("component", request.status, request.reason))
        tenant = self._tenant()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            found = await self._row(
                connection,
                select(COMPONENTS.c.turbine_id).where(
                    COMPONENTS.c.tenant_id == tenant,
                    COMPONENTS.c.component_id == request.component_id,
                ),
                "component",
            )
        turbine_id = str(found.turbine_id)
        user = await self._require_change(await self._scope_of(turbine_id), turbine_id)

        async def work(
            connection: AsyncConnection, now: datetime
        ) -> asset_pb2.SetComponentStatusResponse:
            row = (
                await connection.execute(
                    update(COMPONENTS)
                    .where(
                        COMPONENTS.c.tenant_id == tenant,
                        COMPONENTS.c.component_id == request.component_id,
                    )
                    .values(
                        status=request.status,
                        status_reason=request.reason.strip(),
                        version=COMPONENTS.c.version + 1,
                        updated_at=now,
                    )
                    .returning(*COMPONENTS.c)
                )
            ).one()
            turbine = await self._bump_turbine(connection, turbine_id, now)
            await record_change(
                connection,
                tenant_id=tenant,
                actor_id=user,
                action=f"asset.component.status.{request.status}",
                resource_type="component",
                resource_id=request.component_id,
                turbine=turbine,
            )
            return asset_pb2.SetComponentStatusResponse(component=_component(row))

        return await self._change(
            "SetComponentStatus", request, asset_pb2.SetComponentStatusResponse, user, work
        )
