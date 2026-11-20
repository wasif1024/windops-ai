"""The gRPC API. Authorization decides first, and every query stays in the caller's tenant.

Only a person changes the catalog or stock, or reserves it (FR-INVT-003). The Authorization
Service already refuses an agent anything but reading; this service refuses any identity with an
agent behind it, or with no person, before asking, so an agent's part recommendation can only
ever be a suggestion (FR-INVT-002, SEC-009).
"""

import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.inventory.v1 import inventory_pb2 as pb
from windops.inventory.v1 import inventory_pb2_grpc
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    current_identity,
)
from windops_migrations import in_tenant

from windops_inventory import rules
from windops_inventory.access import Authorizer
from windops_inventory.store import (
    HELD,
    PART_MODELS,
    PARTS,
    RELEASED,
    RESERVATIONS,
    STOCK,
    KeyReusedError,
    announce,
    audit,
    move,
    part_record,
    remember,
    remembered,
    stock_record,
)

READ, UPDATE, RESERVE = "inventory.read", "inventory.update", "inventory.reserve"
STATUSES = (HELD, RELEASED)
DEFAULT_PAGE, MAXIMUM_PAGE, MAXIMUM_KEY, MAXIMUM_QUERY = 50, 200, 64, 200


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def to_stock(row: Any) -> pb.StockLevel:
    return pb.StockLevel(
        part_number=row.part_number,
        wind_farm_id=row.wind_farm_id,
        on_hand=row.on_hand,
        reserved=row.reserved,
        available=row.on_hand - row.reserved,
        version=row.version,
        updated_at=_timestamp(row.updated_at),
    )


def to_reservation(row: Any) -> pb.Reservation:
    return pb.Reservation(
        reservation_id=row.reservation_id,
        work_order_id=row.work_order_id,
        part_number=row.part_number,
        wind_farm_id=row.wind_farm_id,
        quantity=row.quantity,
        status=row.status,
        reserved_by=row.reserved_by,
        reserved_at=_timestamp(row.reserved_at),
        released_by=row.released_by,
        released_at=_timestamp(row.released_at),
        release_reason=row.release_reason,
    )


class InventoryService(inventory_pb2_grpc.InventoryServiceServicer):
    def __init__(
        self,
        engine: AsyncEngine,
        authorizer: Authorizer,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer = engine, authorizer
        self._now = clock or (lambda: datetime.now(UTC))

    # --- who is asking, and what they may do

    @staticmethod
    def _caller() -> tuple[str, str, str]:
        """(tenant, who, actor type)."""
        identity = current_identity()
        who = identity.user_id or identity.subject
        kind = "agent" if identity.actor else ("user" if identity.user_id else "service")
        return identity.tenant_id, who, kind

    @staticmethod
    def _person() -> None:
        """Only a person, with no agent acting for them, changes or reserves stock."""
        identity = current_identity()
        if identity.actor or not identity.user_id:
            raise PermissionDeniedError(
                "Only a person may change or reserve stock: an agent's part recommendation is "
                "a suggestion (FR-INVT-002, FR-INVT-003)"
            )

    async def _reads_catalog(self) -> None:
        if not await self._authorizer.anywhere(READ):
            raise PermissionDeniedError("You do not have permission to read inventory")

    async def _require(self, permission: str, wind_farm_id: str = "") -> None:
        if not await self._authorizer.allowed(permission, wind_farm_id):
            raise PermissionDeniedError("You do not have permission to do this")

    # --- storage

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

    @staticmethod
    async def _parts(
        connection: AsyncConnection, tenant: str, numbers: Iterable[str]
    ) -> dict[str, pb.Part]:
        wanted = sorted(set(numbers))
        if not wanted:
            return {}
        p, m = PARTS.c, PART_MODELS.c
        rows = (
            await connection.execute(
                select(PARTS).where(p.tenant_id == tenant, p.part_number.in_(wanted))
            )
        ).all()
        models = (
            await connection.execute(
                select(PART_MODELS)
                .where(m.tenant_id == tenant, m.part_number.in_(wanted))
                .order_by(m.part_number, m.manufacturer_key, m.model_key)
            )
        ).all()
        fits: dict[str, list[pb.ModelRef]] = {}
        for model in models:
            fits.setdefault(model.part_number, []).append(
                pb.ModelRef(manufacturer=model.manufacturer, model=model.model)
            )
        return {
            row.part_number: pb.Part(
                part_number=row.part_number,
                name=row.name,
                description=row.description,
                component_kind=row.component_kind,
                unit=row.unit,
                fits=fits.get(row.part_number, []),
                fits_any_model=row.fits_any_model,
                retired=row.retired,
                version=row.version,
                updated_at=_timestamp(row.updated_at),
            )
            for row in rows
        }

    @staticmethod
    async def _stock(
        connection: AsyncConnection, tenant: str, part_number: str, wind_farm_id: str
    ) -> Any:
        s = STOCK.c
        return (
            await connection.execute(
                select(STOCK).where(
                    s.tenant_id == tenant,
                    s.part_number == part_number,
                    s.wind_farm_id == wind_farm_id,
                )
            )
        ).one_or_none()

    @staticmethod
    def _fits(part: pb.Part, model: pb.ModelRef) -> bool:
        if part.fits_any_model:
            return True
        wanted = (rules.key(model.manufacturer), rules.key(model.model))
        return any((rules.key(f.manufacturer), rules.key(f.model)) == wanted for f in part.fits)

    # --- the catalog

    async def PutPart(  # noqa: N802  # The name comes from the contract.
        self, request: pb.PutPartRequest, context: Any
    ) -> pb.PutPartResponse:
        self._person()
        tenant, who, kind = self._caller()
        refused = rules.part_problem(request.part)
        if refused:
            raise InvalidRequestError(refused)
        await self._require(UPDATE)
        part, now = request.part, self._now()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                first = await self._first(
                    connection,
                    tenant,
                    who,
                    request.idempotency_key,
                    "PutPart",
                    request,
                    pb.PutPartResponse,
                )
                if first is not None:
                    return first  # type: ignore[no-any-return]
                current = (await self._parts(connection, tenant, [part.part_number])).get(
                    part.part_number
                )
                seen = current.version if current else 0
                if request.expected_version != seen:
                    raise ConflictError(
                        f"{part.part_number} is at version {seen}, not {request.expected_version}"
                    )
                values = {
                    "name": part.name.strip(),
                    "description": part.description.strip(),
                    "component_kind": rules.key(part.component_kind),
                    "unit": part.unit,
                    "fits_any_model": part.fits_any_model,
                    "retired": part.retired,
                    "version": seen + 1,
                    "updated_at": now,
                    "updated_by": who,
                }
                p, m = PARTS.c, PART_MODELS.c
                if current is None:
                    await connection.execute(
                        insert(PARTS).values(
                            tenant_id=tenant, part_number=part.part_number, **values
                        )
                    )
                else:
                    await connection.execute(
                        update(PARTS)
                        .where(p.tenant_id == tenant, p.part_number == part.part_number)
                        .values(**values)
                    )
                    await connection.execute(
                        delete(PART_MODELS).where(
                            m.tenant_id == tenant, m.part_number == part.part_number
                        )
                    )
                models = {(rules.key(f.manufacturer), rules.key(f.model)): f for f in part.fits}
                for (manufacturer_key, model_key), model in sorted(models.items()):
                    await connection.execute(
                        insert(PART_MODELS).values(
                            tenant_id=tenant,
                            part_number=part.part_number,
                            manufacturer_key=manufacturer_key,
                            model_key=model_key,
                            manufacturer=" ".join(model.manufacturer.split()),
                            model=" ".join(model.model.split()),
                        )
                    )
                await audit(
                    connection,
                    tenant,
                    action="part_saved" if current else "part_added",
                    actor_type=kind,
                    actor_id=who,
                    resource_type="part",
                    resource_id=part.part_number,
                )
                await announce(connection, tenant, part_record(part.part_number), seen + 1)
                saved = (await self._parts(connection, tenant, [part.part_number]))[
                    part.part_number
                ]
                response = pb.PutPartResponse(part=saved)
                await remember(
                    connection,
                    tenant_id=tenant,
                    caller=who,
                    key=request.idempotency_key,
                    method="PutPart",
                    request=request,
                    response=response,
                    now=now,
                )
        except IntegrityError as error:
            raise ConflictError(
                f"{part.part_number} changed at the same time; try again"
            ) from error
        return response

    async def GetPart(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetPartRequest, context: Any
    ) -> pb.GetPartResponse:
        tenant = current_identity().tenant_id
        await self._reads_catalog()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            found = await self._parts(connection, tenant, [request.part_number])
        if request.part_number not in found:
            raise NotFoundError("No such part")
        return pb.GetPartResponse(part=found[request.part_number])

    async def ListParts(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListPartsRequest, context: Any
    ) -> pb.ListPartsResponse:
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        if len(request.query) > MAXIMUM_QUERY:
            raise InvalidRequestError(f"query is at most {MAXIMUM_QUERY} characters")
        if request.wind_farm_id and rules.wind_farm_problem(request.wind_farm_id):
            raise InvalidRequestError(rules.wind_farm_problem(request.wind_farm_id))
        tenant = current_identity().tenant_id
        await self._reads_catalog()
        if request.wind_farm_id:
            await self._require(READ, request.wind_farm_id)
        p, m = PARTS.c, PART_MODELS.c
        query = select(p.part_number).where(p.tenant_id == tenant)
        if not request.include_retired:
            query = query.where(p.retired.is_(False))
        if request.component_kind:
            query = query.where(p.component_kind == rules.key(request.component_kind))
        if request.HasField("fits"):
            fitted = select(m.part_number).where(
                m.tenant_id == tenant,
                m.manufacturer_key == rules.key(request.fits.manufacturer),
                m.model_key == rules.key(request.fits.model),
            )
            query = query.where(or_(p.fits_any_model.is_(True), p.part_number.in_(fitted)))
        for word in request.query.lower().split():
            query = query.where(
                or_(
                    func.lower(p.part_number).contains(word, autoescape=True),
                    func.lower(p.name).contains(word, autoescape=True),
                    func.lower(p.description).contains(word, autoescape=True),
                )
            )
        offset = int(request.page_token) if request.page_token.isdigit() else 0
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            numbers = list(
                (
                    await connection.execute(
                        query.order_by(p.part_number).offset(offset).limit(size + 1)
                    )
                ).scalars()
            )
            page = numbers[:size]
            found = await self._parts(connection, tenant, page)
            stock: dict[str, Any] = {}
            if request.wind_farm_id and page:
                s = STOCK.c
                rows = (
                    await connection.execute(
                        select(STOCK).where(
                            s.tenant_id == tenant,
                            s.wind_farm_id == request.wind_farm_id,
                            s.part_number.in_(page),
                        )
                    )
                ).all()
                stock = {row.part_number: row for row in rows}
        listed = []
        for number in page:
            item = pb.ListedPart(part=found[number])
            if request.wind_farm_id:
                row = stock.get(number)
                item.stock.CopyFrom(
                    to_stock(row)
                    if row
                    else pb.StockLevel(part_number=number, wind_farm_id=request.wind_farm_id)
                )
            listed.append(item)
        return pb.ListPartsResponse(
            parts=listed, next_page_token=str(offset + size) if len(numbers) > size else ""
        )

    # --- stock

    async def AdjustStock(  # noqa: N802  # The name comes from the contract.
        self, request: pb.AdjustStockRequest, context: Any
    ) -> pb.AdjustStockResponse:
        self._person()
        tenant, who, kind = self._caller()
        reason = request.reason.strip()
        refused = rules.wind_farm_problem(request.wind_farm_id)
        if not refused and not rules.PART_NUMBER.fullmatch(request.part_number):
            refused = "part_number is not a part number"
        if not refused and not 0 < abs(request.delta) <= rules.MAXIMUM_ON_HAND:
            refused = f"delta is not zero, and at most {rules.MAXIMUM_ON_HAND} either way"
        if not refused and not 0 < len(reason) <= rules.MAXIMUM_REASON:
            refused = f"A reason is needed, of at most {rules.MAXIMUM_REASON} characters"
        if refused:
            raise InvalidRequestError(refused)
        await self._require(UPDATE, request.wind_farm_id)
        now = self._now()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                first = await self._first(
                    connection,
                    tenant,
                    who,
                    request.idempotency_key,
                    "AdjustStock",
                    request,
                    pb.AdjustStockResponse,
                )
                if first is not None:
                    return first  # type: ignore[no-any-return]
                part = (await self._parts(connection, tenant, [request.part_number])).get(
                    request.part_number
                )
                if part is None:
                    raise NotFoundError("No such part")
                if part.retired and request.delta > 0:
                    raise ConflictError(f"{part.part_number} is retired, so no stock is added")
                row = await self._stock(
                    connection, tenant, request.part_number, request.wind_farm_id
                )
                s = STOCK.c
                if row is None:
                    if request.delta < 0:
                        raise ConflictError("The store has none of it to take away")
                    await connection.execute(
                        insert(STOCK).values(
                            tenant_id=tenant,
                            part_number=request.part_number,
                            wind_farm_id=request.wind_farm_id,
                            on_hand=request.delta,
                            reserved=0,
                            version=1,
                            updated_at=now,
                        )
                    )
                else:
                    changed = await connection.execute(
                        update(STOCK)
                        .where(
                            s.tenant_id == tenant,
                            s.part_number == request.part_number,
                            s.wind_farm_id == request.wind_farm_id,
                            s.on_hand + request.delta >= s.reserved,
                        )
                        .values(
                            on_hand=s.on_hand + request.delta,
                            version=s.version + 1,
                            updated_at=now,
                        )
                    )
                    if changed.rowcount != 1:
                        raise ConflictError(
                            f"Only {row.on_hand - row.reserved} are not reserved, so "
                            f"{-request.delta} cannot be taken away"
                        )
                row = await self._stock(
                    connection, tenant, request.part_number, request.wind_farm_id
                )
                await move(
                    connection,
                    row,
                    at=now,
                    kind="adjusted",
                    quantity=request.delta,
                    actor_id=who,
                    reason=reason,
                )
                record = stock_record(row.part_number, row.wind_farm_id)
                await audit(
                    connection,
                    tenant,
                    action="stock_adjusted",
                    actor_type=kind,
                    actor_id=who,
                    resource_type="stock",
                    resource_id=record,
                )
                await announce(connection, tenant, record, row.version)
                response = pb.AdjustStockResponse(stock=to_stock(row))
                await remember(
                    connection,
                    tenant_id=tenant,
                    caller=who,
                    key=request.idempotency_key,
                    method="AdjustStock",
                    request=request,
                    response=response,
                    now=now,
                )
        except IntegrityError as error:
            raise ConflictError("The stock changed at the same time; try again") from error
        return response

    async def GetStock(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetStockRequest, context: Any
    ) -> pb.GetStockResponse:
        tenant = current_identity().tenant_id
        await self._reads_catalog()
        tenant_wide = await self._authorizer.allowed(READ)
        s = STOCK.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            if request.part_number not in await self._parts(
                connection, tenant, [request.part_number]
            ):
                raise NotFoundError("No such part")
            rows = (
                await connection.execute(
                    select(STOCK)
                    .where(s.tenant_id == tenant, s.part_number == request.part_number)
                    .order_by(s.wind_farm_id)
                )
            ).all()
        readable = [
            row
            for row in rows
            if tenant_wide or await self._authorizer.allowed(READ, row.wind_farm_id)
        ]
        return pb.GetStockResponse(stock=[to_stock(row) for row in readable])

    async def CheckParts(  # noqa: N802  # The name comes from the contract.
        self, request: pb.CheckPartsRequest, context: Any
    ) -> pb.CheckPartsResponse:
        refused = rules.wind_farm_problem(request.wind_farm_id) or rules.lines_problem(
            request.lines
        )
        if not refused and not (
            rules.key(request.model.manufacturer) and rules.key(request.model.model)
        ):
            refused = "Name the turbine's manufacturer and model"
        if refused:
            raise InvalidRequestError(refused)
        tenant = current_identity().tenant_id
        await self._require(READ, request.wind_farm_id)
        numbers = [line.part_number for line in request.lines]
        s = STOCK.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            found = await self._parts(connection, tenant, numbers)
            rows = (
                await connection.execute(
                    select(STOCK).where(
                        s.tenant_id == tenant,
                        s.wind_farm_id == request.wind_farm_id,
                        s.part_number.in_(numbers),
                    )
                )
            ).all()
        available = {row.part_number: row.on_hand - row.reserved for row in rows}
        model = f"{request.model.manufacturer} {request.model.model}"
        checks = []
        for line in request.lines:
            part = found.get(line.part_number)
            here = available.get(line.part_number, 0)
            check = pb.PartCheck(
                part_number=line.part_number,
                quantity=line.quantity,
                available=here,
                name=part.name if part else "",
            )
            if part is None:
                check.status, check.message = "unknown", "No such part in the catalog"
            elif part.retired:
                check.status, check.message = "retired", "The part is retired"
            elif not self._fits(part, request.model):
                check.status, check.message = "incompatible", f"It does not fit the {model}"
            elif here >= line.quantity:
                check.status, check.message = "available", f"{here} available"
            else:
                check.status = "short"
                check.message = f"{here} of {line.quantity} available in the store"
            checks.append(check)
        return pb.CheckPartsResponse(checks=checks)

    # --- reservations

    async def ReserveParts(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ReservePartsRequest, context: Any
    ) -> pb.ReservePartsResponse:
        # Before anything else: an agent never reserves, whatever it was granted (SEC-009).
        self._person()
        tenant, who, kind = self._caller()
        refused = rules.wind_farm_problem(request.wind_farm_id) or rules.lines_problem(
            request.lines
        )
        if not refused and not rules.WORK_ORDER.fullmatch(request.work_order_id):
            refused = "work_order_id is a work order's ID"
        if refused:
            raise InvalidRequestError(refused)
        await self._require(RESERVE, request.wind_farm_id)
        now = self._now()
        s = STOCK.c
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                first = await self._first(
                    connection,
                    tenant,
                    who,
                    request.idempotency_key,
                    "ReserveParts",
                    request,
                    pb.ReservePartsResponse,
                )
                if first is not None:
                    return first  # type: ignore[no-any-return]
                found = await self._parts(
                    connection, tenant, [line.part_number for line in request.lines]
                )
                reservations = []
                # Every line, or none: a refusal undoes the lines before it.
                for line in request.lines:
                    part = found.get(line.part_number)
                    if part is None:
                        raise NotFoundError(f"No part {line.part_number}")
                    if part.retired:
                        raise ConflictError(f"{line.part_number} is retired")
                    changed = await connection.execute(
                        update(STOCK)
                        .where(
                            s.tenant_id == tenant,
                            s.part_number == line.part_number,
                            s.wind_farm_id == request.wind_farm_id,
                            s.on_hand - s.reserved >= line.quantity,
                        )
                        .values(
                            reserved=s.reserved + line.quantity,
                            version=s.version + 1,
                            updated_at=now,
                        )
                    )
                    row = await self._stock(
                        connection, tenant, line.part_number, request.wind_farm_id
                    )
                    if changed.rowcount != 1:
                        have = row.on_hand - row.reserved if row else 0
                        raise ConflictError(
                            f"Only {have} of {line.part_number} available in "
                            f"{request.wind_farm_id}'s store, not {line.quantity}"
                        )
                    reservation_id = f"rsv_{secrets.token_hex(10)}"
                    await connection.execute(
                        insert(RESERVATIONS).values(
                            tenant_id=tenant,
                            reservation_id=reservation_id,
                            work_order_id=request.work_order_id,
                            part_number=line.part_number,
                            wind_farm_id=request.wind_farm_id,
                            quantity=line.quantity,
                            status=HELD,
                            reserved_by=who,
                            reserved_at=now,
                        )
                    )
                    await move(
                        connection,
                        row,
                        at=now,
                        kind="reserved",
                        quantity=line.quantity,
                        actor_id=who,
                        reservation_id=reservation_id,
                        reason=request.work_order_id,
                    )
                    await audit(
                        connection,
                        tenant,
                        action="parts_reserved",
                        actor_type=kind,
                        actor_id=who,
                        resource_type="reservation",
                        resource_id=reservation_id,
                    )
                    await announce(
                        connection,
                        tenant,
                        stock_record(line.part_number, request.wind_farm_id),
                        row.version,
                    )
                    reservations.append(await self._reservation(connection, tenant, reservation_id))
                response = pb.ReservePartsResponse(
                    reservations=[to_reservation(r) for r in reservations]
                )
                await remember(
                    connection,
                    tenant_id=tenant,
                    caller=who,
                    key=request.idempotency_key,
                    method="ReserveParts",
                    request=request,
                    response=response,
                    now=now,
                )
        except IntegrityError as error:
            raise ConflictError("The stock changed at the same time; try again") from error
        return response

    @staticmethod
    async def _reservation(connection: AsyncConnection, tenant: str, reservation_id: str) -> Any:
        r = RESERVATIONS.c
        row = (
            await connection.execute(
                select(RESERVATIONS).where(
                    r.tenant_id == tenant, r.reservation_id == reservation_id
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("No such reservation")
        return row

    async def ReleaseReservation(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ReleaseReservationRequest, context: Any
    ) -> pb.ReleaseReservationResponse:
        self._person()
        tenant, who, kind = self._caller()
        reason = request.reason.strip()
        if not 0 < len(reason) <= rules.MAXIMUM_REASON:
            raise InvalidRequestError(
                f"A reason is needed, of at most {rules.MAXIMUM_REASON} characters"
            )
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            first = await self._first(
                connection,
                tenant,
                who,
                request.idempotency_key,
                "ReleaseReservation",
                request,
                pb.ReleaseReservationResponse,
            )
            if first is not None:
                return first  # type: ignore[no-any-return]
            held = await self._reservation(connection, tenant, request.reservation_id)
        # One the caller may not read is not found; one they may read but not release, refused.
        if not await self._authorizer.allowed(READ, held.wind_farm_id):
            raise NotFoundError("No such reservation")
        await self._require(RESERVE, held.wind_farm_id)
        now = self._now()
        s, r = STOCK.c, RESERVATIONS.c
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            held = await self._reservation(connection, tenant, request.reservation_id)
            if held.status != HELD:
                raise ConflictError(f"Only a held reservation is released; it is {held.status}")
            await connection.execute(
                update(RESERVATIONS)
                .where(r.tenant_id == tenant, r.reservation_id == held.reservation_id)
                .values(status=RELEASED, released_by=who, released_at=now, release_reason=reason)
            )
            await connection.execute(
                update(STOCK)
                .where(
                    s.tenant_id == tenant,
                    s.part_number == held.part_number,
                    s.wind_farm_id == held.wind_farm_id,
                )
                .values(reserved=s.reserved - held.quantity, version=s.version + 1, updated_at=now)
            )
            row = await self._stock(connection, tenant, held.part_number, held.wind_farm_id)
            await move(
                connection,
                row,
                at=now,
                kind="released",
                quantity=-held.quantity,
                actor_id=who,
                reservation_id=held.reservation_id,
                reason=reason,
            )
            await audit(
                connection,
                tenant,
                action="reservation_released",
                actor_type=kind,
                actor_id=who,
                resource_type="reservation",
                resource_id=held.reservation_id,
            )
            await announce(
                connection, tenant, stock_record(held.part_number, held.wind_farm_id), row.version
            )
            released = await self._reservation(connection, tenant, held.reservation_id)
            response = pb.ReleaseReservationResponse(reservation=to_reservation(released))
            await remember(
                connection,
                tenant_id=tenant,
                caller=who,
                key=request.idempotency_key,
                method="ReleaseReservation",
                request=request,
                response=response,
                now=now,
            )
        return response

    async def ListReservations(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListReservationsRequest, context: Any
    ) -> pb.ListReservationsResponse:
        if any(s not in STATUSES for s in request.statuses):
            raise InvalidRequestError(f"statuses are of: {', '.join(STATUSES)}")
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        tenant = current_identity().tenant_id
        if request.wind_farm_id:
            await self._require(READ, request.wind_farm_id)
        elif not await self._authorizer.allowed(READ):
            raise PermissionDeniedError("Name a wind farm, or have inventory.read everywhere")
        r = RESERVATIONS.c
        query = select(RESERVATIONS).where(r.tenant_id == tenant)
        if request.wind_farm_id:
            query = query.where(r.wind_farm_id == request.wind_farm_id)
        if request.work_order_id:
            query = query.where(r.work_order_id == request.work_order_id)
        if request.part_number:
            query = query.where(r.part_number == request.part_number)
        if request.statuses:
            query = query.where(r.status.in_(list(request.statuses)))
        offset = int(request.page_token) if request.page_token.isdigit() else 0
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (
                await connection.execute(
                    query.order_by(r.reserved_at.desc(), r.reservation_id)
                    .offset(offset)
                    .limit(size + 1)
                )
            ).all()
        return pb.ListReservationsResponse(
            reservations=[to_reservation(row) for row in rows[:size]],
            next_page_token=str(offset + size) if len(rows) > size else "",
        )
