"""The gRPC API: device credentials. It asks Asset for the turbine, and Authorization first."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.asset.v1 import asset_pb2
from windops.authorization.v1 import authorization_pb2
from windops.telemetry.v1 import telemetry_pb2, telemetry_pb2_grpc
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

from windops_telemetry import credentials
from windops_telemetry.store import DEVICE_CREDENTIALS, KeyReusedError, audit, remember, remembered

MANAGE = "asset.manage"
DEADLINE_SECONDS = 3.0
MAXIMUM_KEY = 64


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...


class Turbines(Protocol):
    async def wind_farm_of(self, turbine_id: str) -> str:
        """The turbine's wind farm, or NotFoundError. Asked as the caller."""
        ...


async def _call(
    client: ServiceClient, method: Callable[..., Any], request: Message, what: str
) -> Any:
    try:
        return await client.call(
            method, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
        )
    except grpc.aio.AioRpcError as error:
        if error.code() is grpc.StatusCode.NOT_FOUND:
            raise NotFoundError("No such turbine") from error
        if error.code() is grpc.StatusCode.PERMISSION_DENIED:
            raise PermissionDeniedError("You do not have permission to do this") from error
        raise UnavailableError(f"{what} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{what} cannot be reached right now") from error


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        return bool((await _call(self._client, self._stub.Check, request, "Authorization")).allowed)


class AssetClient:
    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def wind_farm_of(self, turbine_id: str) -> str:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        answer = await _call(self._client, self._stub.GetTurbine, request, "The Asset Service")
        return str(answer.turbine.wind_farm_id)


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


def _credential(row: Any) -> telemetry_pb2.DeviceCredential:
    return telemetry_pb2.DeviceCredential(
        device_id=row.device_id,
        turbine_id=row.turbine_id,
        created_by=row.created_by,
        created_at=_timestamp(row.created_at),
        revoked_at=_timestamp(row.revoked_at),
    )


class TelemetryService(telemetry_pb2_grpc.TelemetryServiceServicer):
    def __init__(self, engine: AsyncEngine, authorizer: Authorizer, turbines: Turbines) -> None:
        self._engine, self._authorizer, self._turbines = engine, authorizer, turbines

    async def _require_change(self, turbine_id: str) -> str:
        """Return the user, once the turbine exists and they may manage it with a second factor."""
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a user can manage device credentials")
        wind_farm = await self._turbines.wind_farm_of(turbine_id)
        if not await self._authorizer.allowed(MANAGE, wind_farm, turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP_MESSAGE) from error
        return identity.user_id

    @staticmethod
    def _key(key: str) -> None:
        if len(key) > MAXIMUM_KEY:
            raise InvalidRequestError("idempotency_key is too long")

    async def _replay(
        self,
        connection: AsyncConnection,
        tenant: str,
        user: str,
        method: str,
        request: Any,
        kind: Any,
    ) -> Any:
        try:
            stored = await remembered(
                connection, tenant, user, request.idempotency_key, method, request
            )
        except KeyReusedError as error:
            raise ConflictError(str(error)) from error
        return None if stored is None else kind.FromString(stored)

    async def StreamTurbineTelemetry(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.StreamTurbineTelemetryRequest, context: Any
    ) -> AsyncIterator[telemetry_pb2.TelemetryPoint]:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "Reading telemetry arrives with M4.7")
        # Unreachable, but it makes this an async generator, as a stream is.
        yield telemetry_pb2.TelemetryPoint()  # pragma: no cover

    async def CreateTelemetrySnapshot(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.CreateTelemetrySnapshotRequest, context: Any
    ) -> telemetry_pb2.TelemetrySnapshot:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "Snapshots arrive with M4.7")
        raise AssertionError("abort does not return")

    async def CreateDeviceCredential(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.CreateDeviceCredentialRequest, context: Any
    ) -> telemetry_pb2.CreateDeviceCredentialResponse:
        self._key(request.idempotency_key)
        user = await self._require_change(request.turbine_id)
        tenant = current_identity().tenant_id
        now = datetime.now(UTC)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            replay = await self._replay(
                connection, tenant, user, "CreateDeviceCredential", request,
                telemetry_pb2.CreateDeviceCredentialResponse,
            )  # fmt: skip
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            device_id, secret = credentials.new_device_id(), credentials.new_secret()
            row = (
                await connection.execute(
                    DEVICE_CREDENTIALS.insert()
                    .values(
                        tenant_id=tenant,
                        device_id=device_id,
                        turbine_id=request.turbine_id,
                        secret_hash=credentials.hashed(secret),
                        created_by=user,
                        created_at=now,
                    )
                    .returning(*DEVICE_CREDENTIALS.c)
                )
            ).one()
            await audit(
                connection, tenant_id=tenant, actor_id=user,
                action="telemetry.device_credential.created", resource_id=device_id,
            )  # fmt: skip
            # A retry gets the credential, never its secret again.
            await remember(
                connection, tenant_id=tenant, user_id=user, key=request.idempotency_key,
                method="CreateDeviceCredential", request=request,
                response=telemetry_pb2.CreateDeviceCredentialResponse(credential=_credential(row)),
                now=now,
            )  # fmt: skip
        return telemetry_pb2.CreateDeviceCredentialResponse(
            credential=_credential(row), token=credentials.token(tenant, device_id, secret)
        )

    async def ListDeviceCredentials(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.ListDeviceCredentialsRequest, context: Any
    ) -> telemetry_pb2.ListDeviceCredentialsResponse:
        if not await self._authorizer.allowed(MANAGE):
            raise PermissionDeniedError("You do not have permission to do this")
        tenant = current_identity().tenant_id
        query = select(DEVICE_CREDENTIALS).where(DEVICE_CREDENTIALS.c.tenant_id == tenant)
        if request.turbine_id:
            query = query.where(DEVICE_CREDENTIALS.c.turbine_id == request.turbine_id)
        if not request.include_revoked:
            query = query.where(DEVICE_CREDENTIALS.c.revoked_at.is_(None))
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(query.order_by(DEVICE_CREDENTIALS.c.created_at))).all()
        return telemetry_pb2.ListDeviceCredentialsResponse(
            credentials=[_credential(r) for r in rows]
        )

    async def RevokeDeviceCredential(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.RevokeDeviceCredentialRequest, context: Any
    ) -> telemetry_pb2.RevokeDeviceCredentialResponse:
        self._key(request.idempotency_key)
        tenant = current_identity().tenant_id
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            found = (
                await connection.execute(
                    select(DEVICE_CREDENTIALS).where(
                        DEVICE_CREDENTIALS.c.tenant_id == tenant,
                        DEVICE_CREDENTIALS.c.device_id == request.device_id,
                    )
                )
            ).one_or_none()
        if found is None:
            raise NotFoundError("No such device credential")
        user = await self._require_change(found.turbine_id)
        now = datetime.now(UTC)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            replay = await self._replay(
                connection, tenant, user, "RevokeDeviceCredential", request,
                telemetry_pb2.RevokeDeviceCredentialResponse,
            )  # fmt: skip
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            row = (
                await connection.execute(
                    update(DEVICE_CREDENTIALS)
                    .where(
                        DEVICE_CREDENTIALS.c.tenant_id == tenant,
                        DEVICE_CREDENTIALS.c.device_id == request.device_id,
                        DEVICE_CREDENTIALS.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                    .returning(*DEVICE_CREDENTIALS.c)
                )
            ).one_or_none()
            if row is None:
                raise ConflictError("The credential is already revoked")
            await audit(
                connection, tenant_id=tenant, actor_id=user,
                action="telemetry.device_credential.revoked", resource_id=request.device_id,
            )  # fmt: skip
            response = telemetry_pb2.RevokeDeviceCredentialResponse(credential=_credential(row))
            await remember(
                connection, tenant_id=tenant, user_id=user, key=request.idempotency_key,
                method="RevokeDeviceCredential", request=request, response=response, now=now,
            )  # fmt: skip
        return response
