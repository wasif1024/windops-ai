"""The gRPC API: device credentials, reading telemetry, and snapshots (FR-TEL-005, FR-TEL-006).

It asks Asset for the turbine, and Authorization first. Reads stream the rows from PostgreSQL, so a
day's 86,400 samples are never held at once.
"""

import secrets
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import and_, func, insert, select, update
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

from windops_telemetry import credentials, snapshots
from windops_telemetry.quality import DUPLICATE
from windops_telemetry.snapshots import Freezer, Sample
from windops_telemetry.store import (
    DATA_GAPS,
    DEVICE_CREDENTIALS,
    INGEST_REJECTIONS,
    OPERATIONAL_SAMPLES,
    RETENTION_HOLDS,
    SNAPSHOT_SAMPLES,
    TELEMETRY_SNAPSHOTS,
    KeyReusedError,
    audit,
    remember,
    remembered,
)

MANAGE = "asset.manage"
READ = "telemetry.read"
# Rows written at once when a snapshot freezes its samples.
INSERT_BATCH = 2000
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


def _sample(row: Any, label: str) -> Sample:
    return Sample(
        sequence=int(row.sequence),
        event_time=snapshots.utc(row.event_time),
        ingested_at=snapshots.utc(row.ingested_at),
        arrival_index=int(row.arrival_index or 0),
        label=label,
        source=row.source,
        schema_version=row.schema_version,
        model_version=row.model_version,
        synthetic=bool(row.synthetic),
        readings={reading: getattr(row, reading) for reading in snapshots.READING_ORDER},
    )


def _snapshot(row: Any, verified: bool = False) -> telemetry_pb2.TelemetrySnapshot:
    return telemetry_pb2.TelemetrySnapshot(
        snapshot_id=row.snapshot_id,
        content_hash=row.content_hash,
        turbine_id=row.turbine_id,
        from_time=_timestamp(snapshots.utc(row.from_time)),
        to_time=_timestamp(snapshots.utc(row.to_time)),
        created_at=_timestamp(snapshots.utc(row.created_at)),
        created_by=row.created_by,
        sample_count=row.sample_count,
        manifest=telemetry_pb2.SnapshotManifest.FromString(row.manifest),
        released_at=_timestamp(snapshots.utc(row.released_at) if row.released_at else None),
        verified=verified,
    )


# SNAPSHOTS' ORDER: by event time and sequence, then arrival, so a sequence's first copy leads.
SAMPLE_ORDER = (
    OPERATIONAL_SAMPLES.c.event_time,
    OPERATIONAL_SAMPLES.c.sequence,
    OPERATIONAL_SAMPLES.c.ingested_at,
    OPERATIONAL_SAMPLES.c.arrival_index,
)


class TelemetryService(telemetry_pb2_grpc.TelemetryServiceServicer):
    def __init__(
        self, engine: AsyncEngine, authorizer: Authorizer, turbines: Turbines, version: str = ""
    ) -> None:
        self._engine, self._authorizer, self._turbines = engine, authorizer, turbines
        self._version = version

    async def _require_read(self, turbine_id: str) -> None:
        """Once the turbine exists, in the caller's tenant, and they may read its telemetry."""
        if not turbine_id:
            raise InvalidRequestError("turbine_id is required")
        wind_farm = await self._turbines.wind_farm_of(turbine_id)
        if not await self._authorizer.allowed(READ, wind_farm, turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")

    @staticmethod
    def _window(from_time: Any, to_time: Any, *, past: bool = False) -> tuple[datetime, datetime]:
        try:
            return snapshots.window(from_time, to_time, past=past)
        except snapshots.WindowError as error:
            raise InvalidRequestError(str(error)) from error

    @staticmethod
    def _metrics(metrics: Sequence[int]) -> list[tuple[int, str]]:
        try:
            return snapshots.readings_for(metrics)
        except snapshots.WindowError as error:
            raise InvalidRequestError(str(error)) from error

    @staticmethod
    async def _stored(
        connection: AsyncConnection,
        tenant: str,
        turbine: str,
        start: datetime,
        end: datetime,
        ingested_before: datetime | None = None,
    ) -> AsyncIterator[Sample]:
        """The window's stored samples, in ORDER, each with its label now."""
        query = select(OPERATIONAL_SAMPLES).where(
            OPERATIONAL_SAMPLES.c.tenant_id == tenant,
            OPERATIONAL_SAMPLES.c.turbine_id == turbine,
            OPERATIONAL_SAMPLES.c.event_time >= start,
            OPERATIONAL_SAMPLES.c.event_time < end,
        )
        if ingested_before is not None:
            query = query.where(OPERATIONAL_SAMPLES.c.ingested_at < ingested_before)
        result = await connection.stream(query.order_by(*SAMPLE_ORDER))
        async for row in result:
            yield _sample(row, row.quality_status)

    @staticmethod
    async def _frozen(connection: AsyncConnection, snapshot: Any) -> AsyncIterator[Sample]:
        """A snapshot's samples, in ORDER, each with its label when it was frozen."""
        held, stored = SNAPSHOT_SAMPLES, OPERATIONAL_SAMPLES
        query = (
            select(stored, held.c.label.label("frozen_label"))
            .join(
                held,
                and_(
                    held.c.tenant_id == stored.c.tenant_id,
                    held.c.sequence == stored.c.sequence,
                    held.c.ingested_at == stored.c.ingested_at,
                    held.c.arrival_index == func.coalesce(stored.c.arrival_index, 0),
                ),
            )
            .where(
                held.c.snapshot_id == snapshot.snapshot_id,
                stored.c.tenant_id == snapshot.tenant_id,
                stored.c.turbine_id == snapshot.turbine_id,
                stored.c.event_time >= snapshot.from_time,
                stored.c.event_time < snapshot.to_time,
            )
            .order_by(*SAMPLE_ORDER)
        )
        result = await connection.stream(query)
        async for row in result:
            yield _sample(row, row.frozen_label)

    async def _find_snapshot(self, snapshot_id: str) -> Any:
        """The snapshot, once the caller may read its turbine. Another tenant's is not found."""
        tenant = current_identity().tenant_id
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = (
                await connection.execute(
                    select(TELEMETRY_SNAPSHOTS).where(
                        TELEMETRY_SNAPSHOTS.c.tenant_id == tenant,
                        TELEMETRY_SNAPSHOTS.c.snapshot_id == snapshot_id,
                    )
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError("No such snapshot")
        await self._require_read(row.turbine_id)
        return row

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
        start, end = self._window(request.from_time, request.to_time)
        wanted = self._metrics(request.metrics)
        await self._require_read(request.turbine_id)
        tenant = current_identity().tenant_id
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            stored = self._stored(connection, tenant, request.turbine_id, start, end)
            async for sample in _first_copies(stored):
                for point in snapshots.points([sample], wanted):
                    yield point

    async def CreateTelemetrySnapshot(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.CreateTelemetrySnapshotRequest, context: Any
    ) -> telemetry_pb2.TelemetrySnapshot:
        if not request.idempotency_key:
            raise InvalidRequestError("idempotency_key is required")
        self._key(request.idempotency_key)
        start, end = self._window(request.from_time, request.to_time, past=True)
        await self._require_read(request.turbine_id)
        identity = current_identity()
        tenant, caller = identity.tenant_id, identity.user_id or identity.subject
        now = datetime.now(UTC)
        snapshot_id, hold_id = f"tsnap_{secrets.token_hex(10)}", f"hold_{secrets.token_hex(10)}"
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            replay = await self._replay(
                connection, tenant, caller, "CreateTelemetrySnapshot", request,
                telemetry_pb2.TelemetrySnapshot,
            )  # fmt: skip
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            freezer = Freezer(tenant, request.turbine_id, start, end)
            stored = self._stored(connection, tenant, request.turbine_id, start, end, now)
            async for sample in _first_copies(stored):
                freezer.add(sample)
            for offset in range(0, len(freezer.keys), INSERT_BATCH):
                await connection.execute(
                    insert(SNAPSHOT_SAMPLES),
                    [
                        {
                            "tenant_id": tenant,
                            "snapshot_id": snapshot_id,
                            "sequence": sequence,
                            "ingested_at": ingested_at,
                            "arrival_index": arrival,
                            "label": label,
                        }
                        for sequence, ingested_at, arrival, label in freezer.keys[
                            offset : offset + INSERT_BATCH
                        ]
                    ],
                )
            manifest = freezer.manifest(
                gaps=await self._gaps(connection, tenant, request.turbine_id, freezer),
                rejected=await self._rejected(connection, tenant, request.turbine_id, start, end),
                ingested_before=now,
                service_version=self._version,
            )
            await connection.execute(
                insert(RETENTION_HOLDS).values(
                    tenant_id=tenant, hold_id=hold_id, table_name=snapshots.SNAPSHOT_TABLE,
                    starts_at=start, ends_at=end, reason=f"telemetry snapshot {snapshot_id}",
                    created_at=now,
                )
            )  # fmt: skip
            row = (
                await connection.execute(
                    insert(TELEMETRY_SNAPSHOTS)
                    .values(
                        tenant_id=tenant, snapshot_id=snapshot_id,
                        turbine_id=request.turbine_id, from_time=start, to_time=end,
                        created_at=now, created_by=caller, ingested_before=now,
                        sample_count=len(freezer.keys), content_hash=freezer.content_hash,
                        manifest=manifest.SerializeToString(deterministic=True), hold_id=hold_id,
                    )
                    .returning(*TELEMETRY_SNAPSHOTS.c)
                )
            ).one()  # fmt: skip
            await audit(
                connection, tenant_id=tenant, actor_id=caller,
                action="telemetry.snapshot.created", resource_id=snapshot_id,
            )  # fmt: skip
            response = _snapshot(row)
            await remember(
                connection, tenant_id=tenant, user_id=caller, key=request.idempotency_key,
                method="CreateTelemetrySnapshot", request=request, response=response, now=now,
            )  # fmt: skip
        return response

    @staticmethod
    async def _gaps(
        connection: AsyncConnection, tenant: str, turbine: str, freezer: Freezer
    ) -> list[Any]:
        """Runs of sequences the turbine never sent, within the frozen samples' range."""
        if not freezer.keys:
            return []
        first, last = min(k[0] for k in freezer.keys), max(k[0] for k in freezer.keys)
        return list(
            (
                await connection.execute(
                    select(DATA_GAPS).where(
                        DATA_GAPS.c.tenant_id == tenant,
                        DATA_GAPS.c.turbine_id == turbine,
                        DATA_GAPS.c.first_sequence <= last,
                        DATA_GAPS.c.last_sequence >= first,
                    )
                )
            ).all()
        )

    @staticmethod
    async def _rejected(
        connection: AsyncConnection, tenant: str, turbine: str, start: datetime, end: datetime
    ) -> int:
        """Malformed samples in the window that still had a readable time."""
        count = await connection.scalar(
            select(func.count()).where(
                INGEST_REJECTIONS.c.tenant_id == tenant,
                INGEST_REJECTIONS.c.turbine_id == turbine,
                INGEST_REJECTIONS.c.event_time >= start,
                INGEST_REJECTIONS.c.event_time < end,
            )
        )
        return int(count or 0)

    async def GetTelemetrySnapshot(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.GetTelemetrySnapshotRequest, context: Any
    ) -> telemetry_pb2.GetTelemetrySnapshotResponse:
        row = await self._find_snapshot(request.snapshot_id)
        start, end = snapshots.utc(row.from_time), snapshots.utc(row.to_time)
        freezer = Freezer(row.tenant_id, row.turbine_id, start, end)
        async with self._engine.connect() as connection:
            await in_tenant(connection, row.tenant_id)
            async for sample in self._frozen(connection, row):
                freezer.add(sample)
        verified = (
            freezer.content_hash == row.content_hash and len(freezer.keys) == row.sample_count
        )
        return telemetry_pb2.GetTelemetrySnapshotResponse(snapshot=_snapshot(row, verified))

    async def StreamTelemetrySnapshot(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.StreamTelemetrySnapshotRequest, context: Any
    ) -> AsyncIterator[telemetry_pb2.StreamTelemetrySnapshotResponse]:
        wanted = self._metrics(request.metrics)
        row = await self._find_snapshot(request.snapshot_id)
        async with self._engine.connect() as connection:
            await in_tenant(connection, row.tenant_id)
            async for sample in self._frozen(connection, row):
                for point in snapshots.points([sample], wanted):
                    yield telemetry_pb2.StreamTelemetrySnapshotResponse(point=point)

    async def ReleaseTelemetrySnapshot(  # noqa: N802  # The name comes from the contract.
        self, request: telemetry_pb2.ReleaseTelemetrySnapshotRequest, context: Any
    ) -> telemetry_pb2.ReleaseTelemetrySnapshotResponse:
        if not request.idempotency_key:
            raise InvalidRequestError("idempotency_key is required")
        self._key(request.idempotency_key)
        found = await self._find_snapshot(request.snapshot_id)
        identity = current_identity()
        tenant, caller = identity.tenant_id, identity.user_id or identity.subject
        now = datetime.now(UTC)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            replay = await self._replay(
                connection, tenant, caller, "ReleaseTelemetrySnapshot", request,
                telemetry_pb2.ReleaseTelemetrySnapshotResponse,
            )  # fmt: skip
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            row = (
                await connection.execute(
                    update(TELEMETRY_SNAPSHOTS)
                    .where(
                        TELEMETRY_SNAPSHOTS.c.tenant_id == tenant,
                        TELEMETRY_SNAPSHOTS.c.snapshot_id == found.snapshot_id,
                        TELEMETRY_SNAPSHOTS.c.released_at.is_(None),
                    )
                    .values(released_at=now)
                    .returning(*TELEMETRY_SNAPSHOTS.c)
                )
            ).one_or_none()
            if row is None:
                raise ConflictError("The snapshot is already released")
            await connection.execute(
                update(RETENTION_HOLDS)
                .where(
                    RETENTION_HOLDS.c.tenant_id == tenant,
                    RETENTION_HOLDS.c.hold_id == row.hold_id,
                )
                .values(released_at=now)
            )
            await audit(
                connection, tenant_id=tenant, actor_id=caller,
                action="telemetry.snapshot.released", resource_id=row.snapshot_id,
            )  # fmt: skip
            response = telemetry_pb2.ReleaseTelemetrySnapshotResponse(snapshot=_snapshot(row))
            await remember(
                connection, tenant_id=tenant, user_id=caller, key=request.idempotency_key,
                method="ReleaseTelemetrySnapshot", request=request, response=response, now=now,
            )  # fmt: skip
        return response

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


async def _first_copies(samples: AsyncIterator[Sample]) -> AsyncIterator[Sample]:
    """snapshots.first_copies, for samples streamed from the database."""
    seen: set[int] = set()
    async for sample in samples:
        if sample.label == DUPLICATE or sample.sequence in seen:
            continue
        seen.add(sample.sequence)
        yield sample
