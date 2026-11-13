"""The gRPC API. It asks Authorization first, and every query stays in the caller's tenant.

A turbine's wind farm comes from the Asset Service, asked as the caller, because a grant can be
for a wind farm (RBAC-002). A caller who may act across the whole tenant needs no lookup.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.asset.v1 import asset_pb2
from windops.authorization.v1 import authorization_pb2
from windops.investigation.v1 import investigation_pb2 as pb
from windops.investigation.v1 import investigation_pb2_grpc
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

from windops_investigation import case_rules, cases
from windops_investigation.store import (
    ACTIVE,
    CASE_DETECTIONS,
    CASE_HISTORY,
    CASES,
    CLOSED,
    KeyReusedError,
    remember,
    remembered,
)

READ, CREATE, CANCEL = "investigation.read", "investigation.create", "investigation.cancel"
DEADLINE_SECONDS = 3.0
DEFAULT_PAGE, MAXIMUM_PAGE = 100, 500
MAXIMUM_KEY, MAXIMUM_TEXT = 64, 500
STATUSES = (*ACTIVE, CLOSED, "cancelled")


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...


class Assets(Protocol):
    async def wind_farm_of(self, turbine_id: str) -> str: ...


async def _call(client: ServiceClient, method: Any, request: Message, who: str) -> Any:
    try:
        return await client.call(
            method, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
        )
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
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def to_message(row: Any, detection_count: int = 0) -> pb.Case:
    return pb.Case(
        case_id=row.case_id, turbine_id=row.turbine_id, component_group=row.component_group,
        title=row.title, status=row.status, severity=row.severity, origin=row.origin,
        created_by=row.created_by, created_at=_timestamp(row.created_at),
        updated_at=_timestamp(row.updated_at),
        last_detection_at=_timestamp(row.last_detection_at), detection_count=detection_count,
        case_rules_version=row.case_rules_version, cancel_reason=row.cancel_reason,
        version=row.version,
    )  # fmt: skip


class InvestigationService(investigation_pb2_grpc.InvestigationServiceServicer):
    def __init__(
        self,
        engine: AsyncEngine,
        authorizer: Authorizer,
        assets: Assets,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer, self._assets = engine, authorizer, assets
        self._now = clock or (lambda: datetime.now(UTC))

    async def _allowed(self, permission: str, turbine_id: str) -> bool:
        if await self._authorizer.allowed(permission):
            return True
        if not turbine_id:
            return False
        try:
            farm = await self._assets.wind_farm_of(turbine_id)
        except NotFoundError:
            return False
        return await self._authorizer.allowed(permission, farm, turbine_id)

    async def _require(self, permission: str, turbine_id: str) -> None:
        """A case the caller may not read is not found; one they may read, but not act on, is
        refused."""
        if not await self._allowed(READ, turbine_id):
            raise NotFoundError("No such case")
        if permission != READ and not await self._allowed(permission, turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")

    @staticmethod
    def _user() -> tuple[str, str]:
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a person can do this")
        return identity.tenant_id, identity.user_id

    async def CreateCase(  # noqa: N802  # The name comes from the contract.
        self, request: pb.CreateCaseRequest, context: Any
    ) -> pb.CreateCaseResponse:
        title = request.title.strip()
        if not request.turbine_id or not title or len(title) > MAXIMUM_TEXT:
            raise InvalidRequestError(
                f"turbine_id and a title of at most {MAXIMUM_TEXT} are needed"
            )
        if request.severity not in case_rules.SEVERITIES:
            raise InvalidRequestError(f"severity is one of: {', '.join(case_rules.SEVERITIES)}")
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant, user = self._user()
        # Creating needs investigation.create; one who cannot see the turbine cannot use it.
        if not await self._allowed(CREATE, request.turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")
        await self._assets.wind_farm_of(request.turbine_id)
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, user, request.idempotency_key,
                                         "CreateCase", request)  # fmt: skip
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.CreateCaseResponse.FromString(first)
            case = await cases.create(
                connection, tenant=tenant, turbine_id=request.turbine_id,
                group=case_rules.DEFAULT_GROUP, title=title, severity=request.severity,
                origin=cases.USER, by=user, actor_type=cases.USER, now=now,
            )  # fmt: skip
            response = pb.CreateCaseResponse(case=to_message(case))
            await remember(
                connection, tenant_id=tenant, user_id=user, key=request.idempotency_key,
                method="CreateCase", request=request, response=response, now=now,
            )  # fmt: skip
        return response

    async def ListCases(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListCasesRequest, context: Any
    ) -> pb.ListCasesResponse:
        statuses = list(request.statuses) or list(ACTIVE)
        if any(s not in STATUSES for s in statuses):
            raise InvalidRequestError(f"statuses are of: {', '.join(STATUSES)}")
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        if not await self._allowed(READ, request.turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")
        offset = int(request.page_token) if request.page_token.isdigit() else 0
        tenant = current_identity().tenant_id
        c, d = CASES.c, CASE_DETECTIONS.c
        query = select(CASES).where(c.tenant_id == tenant, c.status.in_(statuses))
        if request.turbine_id:
            query = query.where(c.turbine_id == request.turbine_id)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(query)).all()
            counts: dict[str, int] = {}
            for (case_id,) in (await connection.execute(
                select(d.case_id).where(d.tenant_id == tenant)
            )).all():  # fmt: skip
                counts[case_id] = counts.get(case_id, 0) + 1
        rows = sorted(rows, key=lambda r: (-case_rules.rank(r.severity),
                                           -r.updated_at.timestamp(), r.case_id))  # fmt: skip
        page = rows[offset : offset + size]
        return pb.ListCasesResponse(
            cases=[to_message(row, counts.get(row.case_id, 0)) for row in page],
            next_page_token=str(offset + size) if len(rows) > offset + size else "",
        )

    async def GetCase(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetCaseRequest, context: Any
    ) -> pb.GetCaseResponse:
        if not request.case_id:
            raise InvalidRequestError("case_id is required")
        tenant = current_identity().tenant_id
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            case = await cases.load(connection, tenant, request.case_id)
            if case is None:
                raise NotFoundError("No such case")
            await self._require(READ, case.turbine_id)
            detections = (await connection.execute(
                select(CASE_DETECTIONS)
                .where(CASE_DETECTIONS.c.tenant_id == tenant,
                       CASE_DETECTIONS.c.case_id == case.case_id)
                .order_by(CASE_DETECTIONS.c.attached_at, CASE_DETECTIONS.c.detection_id)
            )).all()  # fmt: skip
            history = (await connection.execute(
                select(CASE_HISTORY)
                .where(CASE_HISTORY.c.tenant_id == tenant, CASE_HISTORY.c.case_id == case.case_id)
                .order_by(CASE_HISTORY.c.version)
            )).all()  # fmt: skip
        return pb.GetCaseResponse(
            case=to_message(case, len(detections)),
            detections=[
                pb.CaseDetection(
                    detection_id=d.detection_id, rule_id=d.rule_id, rule_version=d.rule_version,
                    severity=d.severity, telemetry_snapshot_id=d.telemetry_snapshot_id,
                    attached_at=_timestamp(d.attached_at),
                )
                for d in detections
            ],
            history=[
                pb.CaseEvent(at=_timestamp(h.at), action=h.action, actor_type=h.actor_type,
                             actor_id=h.actor_id, note=h.note)
                for h in history
            ],
        )  # fmt: skip

    async def CancelCase(  # noqa: N802  # The name comes from the contract.
        self, request: pb.CancelCaseRequest, context: Any
    ) -> pb.CancelCaseResponse:
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant, user = self._user()
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, user, request.idempotency_key,
                                         "CancelCase", request)  # fmt: skip
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.CancelCaseResponse.FromString(first)
            case = await cases.load(connection, tenant, request.case_id)
            if case is None:
                raise NotFoundError("No such case")
            await self._require(CANCEL, case.turbine_id)
            if case.status not in ACTIVE:
                raise ConflictError(f"Only an active case can be cancelled; it is {case.status}")
            response = pb.CancelCaseResponse(
                case=to_message(await cases.cancel(connection, case, by=user, reason=reason,
                                                   now=now))
            )  # fmt: skip
            await remember(
                connection, tenant_id=tenant, user_id=user, key=request.idempotency_key,
                method="CancelCase", request=request, response=response, now=now,
            )  # fmt: skip
        return response
