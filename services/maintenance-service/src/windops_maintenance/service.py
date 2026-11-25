"""The gRPC API. Authorization decides first, and every query stays in the caller's tenant.

An agent may draft and read, never more (SEC-009, FR-MNT-003). The Authorization Service already
refuses an agent anything else; this service refuses it too, before asking, so no change to the
permission table could ever let an agent submit.
"""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.maintenance.v1 import maintenance_pb2 as pb
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    current_identity,
)
from windops_migrations import in_tenant

from windops_maintenance import plans, validation
from windops_maintenance.store import (
    APPROVED,
    DRAFT,
    REJECTED,
    STALE,
    SUBMITTED,
    WITHDRAWN,
    WORK_ORDER_HISTORY,
    WORK_ORDER_VERSIONS,
    WORK_ORDERS,
    KeyReusedError,
    denied,
    record,
    remember,
    remembered,
)
from windops_maintenance.validation import Authorizer, Sources

READ, DRAFT_PERMISSION, SUBMIT = "investigation.read", "maintenance.draft", "maintenance.submit"
ACTIVE_CASES = ("open", "investigating", "pending_review", "approved")
STATUSES = (DRAFT, SUBMITTED, WITHDRAWN, APPROVED, REJECTED, STALE)
# What is revised as a new version (EVD-008).
REVISABLE = (STALE, REJECTED)
DEFAULT_PAGE, MAXIMUM_PAGE, MAXIMUM_KEY, MAXIMUM_REASON = 50, 200, 64, 500


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def to_message(row: Any) -> pb.WorkOrder:
    return pb.WorkOrder(
        work_order_id=row.work_order_id,
        version=row.version,
        revision=row.revision,
        case_id=row.case_id,
        turbine_id=row.turbine_id,
        evidence_snapshot_id=row.evidence_snapshot_id,
        plan=pb.Plan.FromString(row.plan),
        status=row.status,
        drafted_by=row.drafted_by,
        drafted_by_agent=row.drafted_by_agent,
        created_at=_timestamp(row.created_at),
        updated_at=_timestamp(row.updated_at),
        submitted_by=row.submitted_by,
        submitted_at=_timestamp(row.submitted_at),
        validation=pb.Validation.FromString(row.validation),
        stale_rule=row.stale_rule,
        stale_detail=row.stale_detail,
    )


class MaintenanceService(maintenance_pb2_grpc.MaintenanceServiceServicer):
    def __init__(
        self,
        engine: AsyncEngine,
        authorizer: Authorizer,
        sources: Sources,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer, self._sources = engine, authorizer, sources
        self._now = clock or (lambda: datetime.now(UTC))

    # --- who is asking, and what they may do

    @staticmethod
    def _caller() -> tuple[str, str, str, str]:
        """(tenant, who, actor type, agent). An agent acts for its principal, its subject."""
        identity = current_identity()
        who = identity.user_id or identity.subject
        agent = identity.actor or ""
        kind = "agent" if agent else ("user" if identity.user_id else "service")
        return identity.tenant_id, who, kind, agent

    async def _person(self, permission: str) -> None:
        """Only a person, with no agent acting for them, takes a work order past draft. A
        refusal is recorded, as Authorization would record its own."""
        identity = current_identity()
        if identity.actor or not identity.user_id:
            async with self._engine.begin() as connection:
                await denied(connection, identity, permission)
            raise PermissionDeniedError(
                "Only a person may do this: an agent may only draft (SEC-009, FR-MNT-003)"
            )

    async def _allowed(self, permission: str, turbine_id: str) -> bool:
        if await self._authorizer.allowed(permission):
            return True
        try:
            farm = str((await self._sources.turbine(turbine_id)).wind_farm_id)
        except NotFoundError:
            return False
        return await self._authorizer.allowed(permission, farm, turbine_id)

    async def _require(self, permission: str, turbine_id: str) -> None:
        """One the caller may not read is not found; one they may read but not change, refused."""
        if not await self._allowed(READ, turbine_id):
            raise NotFoundError("No such work order")
        if permission != READ and not await self._allowed(permission, turbine_id):
            raise PermissionDeniedError("You do not have permission to do this")

    # --- storage

    async def _load(self, connection: AsyncConnection, tenant: str, work_order_id: str) -> Any:
        w = WORK_ORDERS.c
        row = (
            await connection.execute(
                select(WORK_ORDERS).where(w.tenant_id == tenant, w.work_order_id == work_order_id)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("No such work order")
        return row

    async def _change(
        self, connection: AsyncConnection, row: Any, now: datetime, **values: Any
    ) -> Any:
        w = WORK_ORDERS.c
        await connection.execute(
            update(WORK_ORDERS)
            .where(w.tenant_id == row.tenant_id, w.work_order_id == row.work_order_id)
            .values(revision=row.revision + 1, updated_at=now, **values)
        )
        return await self._load(connection, row.tenant_id, row.work_order_id)

    @staticmethod
    async def _first(
        connection: AsyncConnection,
        tenant: str,
        who: str,
        key: str,
        method: str,
        request: Any,
        kind: type[Any],
    ) -> Any | None:
        if len(key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        try:
            first = await remembered(connection, tenant, who, key, method, request)
        except KeyReusedError as error:
            raise InvalidRequestError(str(error)) from error
        return None if first is None else kind.FromString(first)

    # --- the API

    async def CreateDraft(  # noqa: N802  # The name comes from the contract.
        self, request: pb.CreateDraftRequest, context: Any
    ) -> pb.CreateDraftResponse:
        tenant, who, kind, agent = self._caller()
        refused = plans.shape(request.plan)
        if not request.case_id or refused:
            raise InvalidRequestError(refused[0].message if refused else "case_id is required")
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            first = await self._first(
                connection,
                tenant,
                who,
                request.idempotency_key,
                "CreateDraft",
                request,
                pb.CreateDraftResponse,
            )
        if first is not None:
            return first  # type: ignore[no-any-return]
        case = await self._sources.case(request.case_id)
        await self._require(DRAFT_PERMISSION, case.turbine_id)
        if case.status not in ACTIVE_CASES:
            raise ConflictError(f"Only an active case gets a work order; it is {case.status}")
        work_order_id, now = f"wo_{secrets.token_hex(10)}", self._now()
        checked = await validation.validate(
            self._sources, case, request.plan, key=f"{work_order_id}-1", now=now
        )
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await connection.execute(
                WORK_ORDERS.insert().values(
                    tenant_id=tenant,
                    work_order_id=work_order_id,
                    version=1,
                    revision=1,
                    case_id=case.case_id,
                    turbine_id=case.turbine_id,
                    evidence_snapshot_id=case.evidence_snapshot_id,
                    plan=request.plan.SerializeToString(),
                    validation=checked.SerializeToString(),
                    status=DRAFT,
                    drafted_by=who,
                    drafted_by_agent=agent,
                    created_at=now,
                    updated_at=now,
                )
            )
            row = await self._load(connection, tenant, work_order_id)
            await record(
                connection,
                row,
                action="drafted",
                at=now,
                actor_type=kind,
                actor_id=who,
                agent=agent,
                note=case.case_id,
            )
            response = pb.CreateDraftResponse(work_order=to_message(row))
            await remember(
                connection,
                tenant_id=tenant,
                caller=who,
                key=request.idempotency_key,
                method="CreateDraft",
                request=request,
                response=response,
                now=now,
            )
        return response

    async def UpdateDraft(  # noqa: N802  # The name comes from the contract.
        self, request: pb.UpdateDraftRequest, context: Any
    ) -> pb.UpdateDraftResponse:
        tenant, who, kind, agent = self._caller()
        refused = plans.shape(request.plan)
        if refused:
            raise InvalidRequestError(refused[0].message)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            first = await self._first(
                connection,
                tenant,
                who,
                request.idempotency_key,
                "UpdateDraft",
                request,
                pb.UpdateDraftResponse,
            )
            if first is not None:
                return first  # type: ignore[no-any-return]
            row = await self._load(connection, tenant, request.work_order_id)
        await self._require(DRAFT_PERMISSION, row.turbine_id)
        case = await self._sources.case(row.case_id)
        now = self._now()
        checked = await validation.validate(
            self._sources,
            case,
            request.plan,
            key=f"{row.work_order_id}-{row.revision + 1}",
            now=now,
        )
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.work_order_id)
            self._editable(row, request.expected_revision)
            row = await self._change(
                connection,
                row,
                now,
                plan=request.plan.SerializeToString(),
                validation=checked.SerializeToString(),
            )
            await record(
                connection,
                row,
                action="updated",
                at=now,
                actor_type=kind,
                actor_id=who,
                agent=agent,
            )
            response = pb.UpdateDraftResponse(work_order=to_message(row))
            await remember(
                connection,
                tenant_id=tenant,
                caller=who,
                key=request.idempotency_key,
                method="UpdateDraft",
                request=request,
                response=response,
                now=now,
            )
        return response

    @staticmethod
    def _editable(row: Any, expected_revision: int) -> None:
        if row.status != DRAFT:
            raise ConflictError(f"Only a draft changes; this one is {row.status}")
        if expected_revision != row.revision:
            raise ConflictError(
                f"It changed since you read it: revision {row.revision}, not {expected_revision}"
            )

    async def ValidateWorkOrder(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ValidateWorkOrderRequest, context: Any
    ) -> pb.ValidateWorkOrderResponse:
        tenant, who, kind, agent = self._caller()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.work_order_id)
        await self._require(DRAFT_PERMISSION, row.turbine_id)
        if row.status != DRAFT:
            raise ConflictError(f"Only a draft is validated; this one is {row.status}")
        case = await self._sources.case(row.case_id)
        now = self._now()
        checked = await validation.validate(
            self._sources,
            case,
            pb.Plan.FromString(row.plan),
            key=f"{row.work_order_id}-{row.revision}",
            now=now,
        )
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.work_order_id)
            w = WORK_ORDERS.c
            await connection.execute(
                update(WORK_ORDERS)
                .where(w.tenant_id == tenant, w.work_order_id == row.work_order_id)
                .values(validation=checked.SerializeToString())
            )
            row = await self._load(connection, tenant, request.work_order_id)
            await record(
                connection,
                row,
                action="validated",
                at=now,
                actor_type=kind,
                actor_id=who,
                agent=agent,
                note="passed" if checked.passed else "blocked",
            )
        return pb.ValidateWorkOrderResponse(work_order=to_message(row))

    async def SubmitWorkOrder(  # noqa: N802  # The name comes from the contract.
        self, request: pb.SubmitWorkOrderRequest, context: Any
    ) -> pb.SubmitWorkOrderResponse:
        # Before anything else: an agent never submits, whatever it was granted (SEC-009).
        await self._person(SUBMIT)
        tenant, who, kind, _ = self._caller()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            first = await self._first(
                connection,
                tenant,
                who,
                request.idempotency_key,
                "SubmitWorkOrder",
                request,
                pb.SubmitWorkOrderResponse,
            )
            if first is not None:
                return first  # type: ignore[no-any-return]
            row = await self._load(connection, tenant, request.work_order_id)
        await self._require(SUBMIT, row.turbine_id)
        self._editable(row, request.expected_revision)
        case = await self._sources.case(row.case_id)
        now = self._now()
        checked = await validation.validate(
            self._sources,
            case,
            pb.Plan.FromString(row.plan),
            key=f"{row.work_order_id}-{row.revision}",
            now=now,
        )
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.work_order_id)
            self._editable(row, request.expected_revision)
            if not checked.passed:
                # Kept, so the planner sees why; the submission does not go ahead.
                w = WORK_ORDERS.c
                await connection.execute(
                    update(WORK_ORDERS)
                    .where(w.tenant_id == tenant, w.work_order_id == row.work_order_id)
                    .values(validation=checked.SerializeToString())
                )
                blocking = [p.message for p in checked.problems if p.blocking]
                failed = "It cannot be submitted yet: " + "; ".join(blocking)
            else:
                row = await self._change(
                    connection,
                    row,
                    now,
                    status=SUBMITTED,
                    submitted_by=who,
                    submitted_at=now,
                    validation=checked.SerializeToString(),
                )
                await record(
                    connection, row, action="submitted", at=now, actor_type=kind, actor_id=who
                )
                response = pb.SubmitWorkOrderResponse(work_order=to_message(row))
                await remember(
                    connection,
                    tenant_id=tenant,
                    caller=who,
                    key=request.idempotency_key,
                    method="SubmitWorkOrder",
                    request=request,
                    response=response,
                    now=now,
                )
                return response
        raise ConflictError(failed[:1000])

    async def WithdrawDraft(  # noqa: N802  # The name comes from the contract.
        self, request: pb.WithdrawDraftRequest, context: Any
    ) -> pb.WithdrawDraftResponse:
        await self._person(DRAFT_PERMISSION)
        tenant, who, kind, _ = self._caller()
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_REASON:
            raise InvalidRequestError(f"A reason is needed, of at most {MAXIMUM_REASON}")
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            first = await self._first(
                connection,
                tenant,
                who,
                request.idempotency_key,
                "WithdrawDraft",
                request,
                pb.WithdrawDraftResponse,
            )
            if first is not None:
                return first  # type: ignore[no-any-return]
            row = await self._load(connection, tenant, request.work_order_id)
            await self._require(DRAFT_PERMISSION, row.turbine_id)
            if row.status != DRAFT:
                raise ConflictError(f"Only a draft is withdrawn; this one is {row.status}")
            now = self._now()
            row = await self._change(connection, row, now, status=WITHDRAWN)
            await record(
                connection,
                row,
                action="withdrawn",
                at=now,
                actor_type=kind,
                actor_id=who,
                note=reason,
            )
            response = pb.WithdrawDraftResponse(work_order=to_message(row))
            await remember(
                connection,
                tenant_id=tenant,
                caller=who,
                key=request.idempotency_key,
                method="WithdrawDraft",
                request=request,
                response=response,
                now=now,
            )
        return response

    async def ReviseWorkOrder(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ReviseWorkOrderRequest, context: Any
    ) -> pb.ReviseWorkOrderResponse:
        tenant, who, kind, agent = self._caller()
        if request.HasField("plan") and (refused := plans.shape(request.plan)):
            raise InvalidRequestError(refused[0].message)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            first = await self._first(
                connection, tenant, who, request.idempotency_key, "ReviseWorkOrder", request,
                pb.ReviseWorkOrderResponse,
            )  # fmt: skip
            if first is not None:
                return first  # type: ignore[no-any-return]
            row = await self._load(connection, tenant, request.work_order_id)
        await self._require(DRAFT_PERMISSION, row.turbine_id)
        self._revisable(row, request.expected_revision)
        case = await self._sources.case(row.case_id)
        if case.status not in ACTIVE_CASES:
            raise ConflictError(f"Only an active case's work is revised; it is {case.status}")
        if case.stale_rule:
            raise ConflictError(
                f"The case's evidence is stale ({case.stale_rule}): re-evaluate the case first, "
                "so the new version stands on a fresh snapshot"
            )
        plan = request.plan if request.HasField("plan") else pb.Plan.FromString(row.plan)
        now, version = self._now(), row.version + 1
        checked = await validation.validate(
            self._sources, case, plan, key=f"{row.work_order_id}-v{version}", now=now
        )
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.work_order_id)
            self._revisable(row, request.expected_revision)
            # The earlier version, kept as it was (EVD-008).
            await connection.execute(WORK_ORDER_VERSIONS.insert().values(
                tenant_id=tenant, work_order_id=row.work_order_id, version=row.version,
                work_order=to_message(row).SerializeToString(), revised_at=now,
            ))  # fmt: skip
            row = await self._change(
                connection, row, now, version=version, status=DRAFT,
                evidence_snapshot_id=case.evidence_snapshot_id,
                plan=plan.SerializeToString(), validation=checked.SerializeToString(),
                submitted_by="", submitted_at=None, stale_rule="", stale_detail="",
            )  # fmt: skip
            await record(connection, row, action="revised", at=now, actor_type=kind,
                         actor_id=who, agent=agent, note=f"version {version}")  # fmt: skip
            response = pb.ReviseWorkOrderResponse(work_order=to_message(row))
            await remember(connection, tenant_id=tenant, caller=who,
                           key=request.idempotency_key, method="ReviseWorkOrder",
                           request=request, response=response, now=now)  # fmt: skip
        return response

    @staticmethod
    def _revisable(row: Any, expected_revision: int) -> None:
        if row.status not in REVISABLE:
            raise ConflictError(f"Only a stale or rejected work order is revised; this one is "
                                f"{row.status}")  # fmt: skip
        if expected_revision != row.revision:
            raise ConflictError(
                f"It changed since you read it: revision {row.revision}, not {expected_revision}"
            )

    async def GetWorkOrder(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetWorkOrderRequest, context: Any
    ) -> pb.GetWorkOrderResponse:
        tenant = current_identity().tenant_id
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.work_order_id)
            await self._require(READ, row.turbine_id)
            h = WORK_ORDER_HISTORY.c
            history = (
                await connection.execute(
                    select(WORK_ORDER_HISTORY)
                    .where(h.tenant_id == tenant, h.work_order_id == row.work_order_id)
                    .order_by(h.position)
                )
            ).all()
            v = WORK_ORDER_VERSIONS.c
            earlier = (await connection.execute(
                select(v.work_order)
                .where(v.tenant_id == tenant, v.work_order_id == row.work_order_id)
                .order_by(v.version)
            )).scalars().all()  # fmt: skip
        return pb.GetWorkOrderResponse(
            work_order=to_message(row),
            earlier_versions=[pb.WorkOrder.FromString(e) for e in earlier],
            history=[
                pb.WorkOrderEvent(
                    at=_timestamp(e.at),
                    action=e.action,
                    actor_type=e.actor_type,
                    actor_id=e.actor_id,
                    agent=e.agent,
                    revision=e.revision,
                    note=e.note,
                )
                for e in history
            ],
        )

    async def ListWorkOrders(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListWorkOrdersRequest, context: Any
    ) -> pb.ListWorkOrdersResponse:
        if any(s not in STATUSES for s in request.statuses):
            raise InvalidRequestError(f"statuses are of: {', '.join(STATUSES)}")
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        tenant = current_identity().tenant_id
        tenant_wide = await self._authorizer.allowed(READ)
        if not tenant_wide and not request.turbine_id:
            raise PermissionDeniedError("Name a turbine, or have investigation.read everywhere")
        if not tenant_wide:
            await self._require(READ, request.turbine_id)
        w = WORK_ORDERS.c
        query = select(WORK_ORDERS).where(w.tenant_id == tenant)
        if request.case_id:
            query = query.where(w.case_id == request.case_id)
        if request.turbine_id:
            query = query.where(w.turbine_id == request.turbine_id)
        if request.statuses:
            query = query.where(w.status.in_(list(request.statuses)))
        offset = int(request.page_token) if request.page_token.isdigit() else 0
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (
                await connection.execute(
                    query.order_by(w.created_at.desc(), w.work_order_id)
                    .offset(offset)
                    .limit(size + 1)
                )
            ).all()
        return pb.ListWorkOrdersResponse(
            work_orders=[to_message(r) for r in rows[:size]],
            next_page_token=str(offset + size) if len(rows) > size else "",
        )
