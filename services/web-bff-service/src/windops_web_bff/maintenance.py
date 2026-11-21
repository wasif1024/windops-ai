"""The maintenance planner and parts lookup's API: work orders and spare parts (M9.4, SRS 4.11).

It decides nothing. The Maintenance Service decides who may draft, validates every plan, and lets
only a person submit (FR-MNT-002, FR-MNT-003); the Inventory Service decides who reads which store
(FR-INVT-001). Each call is made as the signed-in user. Changes are never retried here: the
browser resends one with the same idempotency key.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import AwareDatetime, BaseModel, Field
from windops.asset.v1 import asset_pb2
from windops.inventory.v1 import inventory_pb2 as inv
from windops.investigation.v1 import investigation_pb2
from windops.maintenance.v1 import maintenance_pb2 as mnt

from windops_web_bff.common import ID_PATTERN, Call, Key, time_of
from windops_web_bff.tokens import Caller

WorkOrderId = Annotated[str, Path(pattern=r"^wo_[a-z0-9]{20}$")]
PART_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{1,63}$"
ACTION_PATTERN = r"^(inspection|repair|replacement|adjustment|monitoring)$"
STATUSES = ("draft", "submitted", "withdrawn", "approved", "rejected")


# --- What the browser sends


class TaskIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    duration_hours: float = Field(gt=0, le=240)
    crew_size: int = Field(ge=1, le=20)
    requires_climb: bool


class PartLineIn(BaseModel):
    part_number: str = Field(pattern=PART_PATTERN)
    quantity: int = Field(ge=1, le=1000)


class PlanIn(BaseModel):
    """A plan as the planner writes it. The Maintenance Service checks it in full; this only
    keeps it to a sane size."""

    title: str = Field(max_length=200)
    action: str = Field(pattern=ACTION_PATTERN)
    summary: str = Field(max_length=4000)
    tasks: list[TaskIn] = Field(max_length=50)
    parts: list[PartLineIn] = Field(default_factory=list, max_length=50)
    procedures: list[str] = Field(default_factory=list, max_length=20)
    planned_start: AwareDatetime | None = None
    planned_end: AwareDatetime | None = None
    estimated_downtime_hours: float = Field(ge=0, le=720)
    impact_result_id: str = Field("", max_length=64)


class NewDraft(BaseModel):
    case_id: str = Field(pattern=ID_PATTERN)
    plan: PlanIn


class PlanChange(BaseModel):
    expected_revision: int = Field(ge=1)
    plan: PlanIn


class Submission(BaseModel):
    expected_revision: int = Field(ge=1)


class Withdrawal(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


# --- What it answers


class Task(TaskIn):
    pass


class PartLine(BaseModel):
    part_number: str
    quantity: int


class Plan(BaseModel):
    title: str
    action: str
    summary: str
    tasks: list[Task]
    parts: list[PartLine]
    procedures: list[str]
    planned_start: str | None
    planned_end: str | None
    estimated_downtime_hours: float
    impact_result_id: str


class Problem(BaseModel):
    field: str
    code: str
    message: str
    blocking: bool


class PartAvailability(BaseModel):
    part_number: str
    quantity: int
    # "available", "short", "incompatible", "retired" or "unknown".
    status: str
    available: int


class Validation(BaseModel):
    checked_at: str | None
    passed: bool
    problems: list[Problem]
    turbine_status: str
    # "pass", "escalate" or "block"; "" if the Policy Service could not be asked.
    policy_outcome: str
    policy_result_id: str
    parts: list[PartAvailability]


class WorkOrder(BaseModel):
    work_order_id: str
    version: int
    revision: int
    case_id: str
    turbine_id: str
    evidence_snapshot_id: str
    plan: Plan
    # "draft", "submitted", "withdrawn", "approved" or "rejected".
    status: str
    drafted_by: str
    # "agent:maintenance-planner" when an agent drafted it; "" for a person.
    drafted_by_agent: str
    created_at: str | None
    updated_at: str | None
    submitted_by: str
    submitted_at: str | None
    validation: Validation


class WorkOrderEvent(BaseModel):
    at: str | None
    action: str
    actor_type: str
    actor_id: str
    agent: str
    revision: int
    note: str


class WorkOrderList(BaseModel):
    work_orders: list[WorkOrder]
    next_page_token: str


class WorkOrderDetail(BaseModel):
    work_order: WorkOrder
    history: list[WorkOrderEvent]


class Model(BaseModel):
    manufacturer: str
    model: str


class Part(BaseModel):
    part_number: str
    name: str
    description: str
    component_kind: str
    unit: str
    fits: list[Model]
    fits_any_model: bool
    retired: bool
    version: int


class Stock(BaseModel):
    wind_farm_id: str
    on_hand: int
    reserved: int
    available: int
    updated_at: str | None


class ListedPart(BaseModel):
    part: Part
    # The turbine's wind farm's store, when the search named a turbine.
    stock: Stock | None


class PartSearch(BaseModel):
    # The turbine the parts fit, when the search named one.
    turbine_id: str
    wind_farm_id: str
    model: Model | None
    parts: list[ListedPart]
    next_page_token: str


class PartDetail(BaseModel):
    part: Part
    # Each store the user may read.
    stock: list[Stock]


class ActiveCase(BaseModel):
    case_id: str
    turbine_id: str
    title: str
    status: str
    severity: str


# --- Translation


def _stamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


def plan_message(plan: PlanIn) -> mnt.Plan:
    return mnt.Plan(
        title=plan.title, action=plan.action, summary=plan.summary,
        tasks=[mnt.Task(description=t.description, duration_hours=t.duration_hours,
                        crew_size=t.crew_size, requires_climb=t.requires_climb)
               for t in plan.tasks],
        parts=[mnt.PartLine(part_number=p.part_number, quantity=p.quantity) for p in plan.parts],
        procedures=plan.procedures, planned_start=_stamp(plan.planned_start),
        planned_end=_stamp(plan.planned_end),
        estimated_downtime_hours=plan.estimated_downtime_hours,
        impact_result_id=plan.impact_result_id,
    )  # fmt: skip


def _work_order(message: Any) -> WorkOrder:
    plan, checked = message.plan, message.validation
    return WorkOrder(
        work_order_id=message.work_order_id, version=message.version, revision=message.revision,
        case_id=message.case_id, turbine_id=message.turbine_id,
        evidence_snapshot_id=message.evidence_snapshot_id,
        plan=Plan(
            title=plan.title, action=plan.action, summary=plan.summary,
            tasks=[Task(description=t.description, duration_hours=t.duration_hours,
                        crew_size=t.crew_size, requires_climb=t.requires_climb)
                   for t in plan.tasks],
            parts=[PartLine(part_number=p.part_number, quantity=p.quantity) for p in plan.parts],
            procedures=list(plan.procedures), planned_start=time_of(plan, "planned_start"),
            planned_end=time_of(plan, "planned_end"),
            estimated_downtime_hours=plan.estimated_downtime_hours,
            impact_result_id=plan.impact_result_id,
        ),
        status=message.status, drafted_by=message.drafted_by,
        drafted_by_agent=message.drafted_by_agent, created_at=time_of(message, "created_at"),
        updated_at=time_of(message, "updated_at"), submitted_by=message.submitted_by,
        submitted_at=time_of(message, "submitted_at"),
        validation=Validation(
            checked_at=time_of(checked, "checked_at"), passed=checked.passed,
            problems=[Problem(field=p.field, code=p.code, message=p.message, blocking=p.blocking)
                      for p in checked.problems],
            turbine_status=checked.turbine_status, policy_outcome=checked.policy_outcome,
            policy_result_id=checked.policy_result_id,
            parts=[PartAvailability(part_number=p.part_number, quantity=p.quantity,
                                    status=p.status, available=p.available)
                   for p in checked.parts],
        ),
    )  # fmt: skip


def _part(message: Any) -> Part:
    return Part(
        part_number=message.part_number, name=message.name, description=message.description,
        component_kind=message.component_kind, unit=message.unit,
        fits=[Model(manufacturer=m.manufacturer, model=m.model) for m in message.fits],
        fits_any_model=message.fits_any_model, retired=message.retired, version=message.version,
    )  # fmt: skip


def _stock(message: Any) -> Stock:
    return Stock(wind_farm_id=message.wind_farm_id, on_hand=message.on_hand,
                 reserved=message.reserved, available=message.available,
                 updated_at=time_of(message, "updated_at"))  # fmt: skip


def add_maintenance_routes(
    router: APIRouter, caller: Callable[..., Awaitable[Caller]], call_as: Call
) -> None:
    # --- Work orders

    @router.get(
        "/work-orders", response_model=WorkOrderList,
        summary="Work orders the user may read, newest first, by case, turbine or status",
    )  # fmt: skip
    async def list_work_orders(
        who: Annotated[Caller, Depends(caller)],
        case_id: Annotated[str, Query(max_length=64)] = "",
        turbine_id: Annotated[str, Query(max_length=64)] = "",
        status: Annotated[list[str] | None, Query(max_length=3)] = None,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(max_length=64)] = "",
    ) -> WorkOrderList:
        request = mnt.ListWorkOrdersRequest(
            case_id=case_id, turbine_id=turbine_id,
            statuses=[s for s in status or [] if s in STATUSES], page_size=page_size,
            page_token=page_token,
        )  # fmt: skip
        answer = await call_as("maintenance", "ListWorkOrders", request, who)
        return WorkOrderList(work_orders=[_work_order(w) for w in answer.work_orders],
                             next_page_token=answer.next_page_token)  # fmt: skip

    # The cases a draft may be made for. Before /work-orders/{id}, which would take its name.

    @router.get(
        "/work-orders/cases", response_model=list[ActiveCase],
        summary="Active cases the user may read, most severe and newest first, to draft for",
    )  # fmt: skip
    async def active_cases(
        who: Annotated[Caller, Depends(caller)],
        turbine_id: Annotated[str, Query(max_length=64)] = "",
    ) -> list[ActiveCase]:
        answer = await call_as(
            "investigation", "ListCases",
            investigation_pb2.ListCasesRequest(turbine_id=turbine_id, page_size=100), who,
        )  # fmt: skip
        return [ActiveCase(case_id=c.case_id, turbine_id=c.turbine_id, title=c.title,
                           status=c.status, severity=c.severity) for c in answer.cases]  # fmt: skip

    @router.post(
        "/work-orders", response_model=WorkOrder, status_code=201,
        summary="Draft a work order for an active case. It is validated at once",
    )  # fmt: skip
    async def draft(body: NewDraft, key: Key, who: Annotated[Caller, Depends(caller)]) -> WorkOrder:
        request = mnt.CreateDraftRequest(case_id=body.case_id, plan=plan_message(body.plan),
                                         idempotency_key=key)  # fmt: skip
        answer = await call_as("maintenance", "CreateDraft", request, who, retry=False)
        return _work_order(answer.work_order)

    @router.get(
        "/work-orders/{work_order_id}", response_model=WorkOrderDetail,
        summary="One work order, with every change to it",
    )  # fmt: skip
    async def get_work_order(
        work_order_id: WorkOrderId, who: Annotated[Caller, Depends(caller)]
    ) -> WorkOrderDetail:
        answer = await call_as(
            "maintenance", "GetWorkOrder", mnt.GetWorkOrderRequest(work_order_id=work_order_id),
            who,
        )  # fmt: skip
        return WorkOrderDetail(
            work_order=_work_order(answer.work_order),
            history=[WorkOrderEvent(at=time_of(e, "at"), action=e.action,
                                    actor_type=e.actor_type, actor_id=e.actor_id, agent=e.agent,
                                    revision=e.revision, note=e.note)
                     for e in answer.history],
        )  # fmt: skip

    @router.put(
        "/work-orders/{work_order_id}/plan", response_model=WorkOrder,
        summary="Replace a draft's plan, from the revision the user saw. It is validated again",
    )  # fmt: skip
    async def update(
        work_order_id: WorkOrderId, body: PlanChange, key: Key,
        who: Annotated[Caller, Depends(caller)],
    ) -> WorkOrder:  # fmt: skip
        request = mnt.UpdateDraftRequest(
            work_order_id=work_order_id, expected_revision=body.expected_revision,
            plan=plan_message(body.plan), idempotency_key=key,
        )  # fmt: skip
        answer = await call_as("maintenance", "UpdateDraft", request, who, retry=False)
        return _work_order(answer.work_order)

    @router.post(
        "/work-orders/{work_order_id}/validate", response_model=WorkOrder,
        summary="Check a draft again now: its plan, turbine, parts and the Policy Service's rules",
    )  # fmt: skip
    async def validate(
        work_order_id: WorkOrderId, who: Annotated[Caller, Depends(caller)]
    ) -> WorkOrder:
        # It changes nothing but the check it keeps, so it needs no idempotency key.
        request = mnt.ValidateWorkOrderRequest(work_order_id=work_order_id)
        answer = await call_as("maintenance", "ValidateWorkOrder", request, who, retry=False)
        return _work_order(answer.work_order)

    @router.post(
        "/work-orders/{work_order_id}/submit", response_model=WorkOrder,
        summary="Submit a draft for a decision, once nothing blocks it. Only a person",
    )  # fmt: skip
    async def submit(
        work_order_id: WorkOrderId, body: Submission, key: Key,
        who: Annotated[Caller, Depends(caller)],
    ) -> WorkOrder:  # fmt: skip
        request = mnt.SubmitWorkOrderRequest(work_order_id=work_order_id,
                                             expected_revision=body.expected_revision,
                                             idempotency_key=key)  # fmt: skip
        answer = await call_as("maintenance", "SubmitWorkOrder", request, who, retry=False)
        return _work_order(answer.work_order)

    @router.post(
        "/work-orders/{work_order_id}/withdraw", response_model=WorkOrder,
        summary="Withdraw a draft, with a reason",
    )  # fmt: skip
    async def withdraw(
        work_order_id: WorkOrderId, body: Withdrawal, key: Key,
        who: Annotated[Caller, Depends(caller)],
    ) -> WorkOrder:  # fmt: skip
        request = mnt.WithdrawDraftRequest(work_order_id=work_order_id, reason=body.reason,
                                           idempotency_key=key)  # fmt: skip
        answer = await call_as("maintenance", "WithdrawDraft", request, who, retry=False)
        return _work_order(answer.work_order)

    # --- Parts

    @router.get(
        "/parts", response_model=PartSearch,
        summary="Spare parts by words or component, or those that fit a turbine, with its store's "
        "stock",
    )  # fmt: skip
    async def search_parts(
        who: Annotated[Caller, Depends(caller)],
        turbine_id: Annotated[str, Query(max_length=64)] = "",
        component_kind: Annotated[str, Query(max_length=64)] = "",
        query: Annotated[str, Query(max_length=200)] = "",
        include_retired: bool = False,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(max_length=64)] = "",
    ) -> PartSearch:
        request = inv.ListPartsRequest(component_kind=component_kind, query=query,
                                       include_retired=include_retired, page_size=page_size,
                                       page_token=page_token)  # fmt: skip
        turbine = None
        if turbine_id:
            turbine = (await call_as("asset", "GetTurbine",
                                     asset_pb2.GetTurbineRequest(turbine_id=turbine_id),
                                     who)).turbine  # fmt: skip
            request.fits.CopyFrom(inv.ModelRef(manufacturer=turbine.model.manufacturer,
                                               model=turbine.model.model))  # fmt: skip
            request.wind_farm_id = turbine.wind_farm_id
        answer = await call_as("inventory", "ListParts", request, who)
        return PartSearch(
            turbine_id=turbine.turbine_id if turbine else "",
            wind_farm_id=turbine.wind_farm_id if turbine else "",
            model=Model(manufacturer=turbine.model.manufacturer, model=turbine.model.model)
            if turbine else None,
            parts=[ListedPart(part=_part(p.part),
                              stock=_stock(p.stock) if p.HasField("stock") else None)
                   for p in answer.parts],
            next_page_token=answer.next_page_token,
        )  # fmt: skip

    @router.get(
        "/parts/detail", response_model=PartDetail,
        summary="One part, and its stock in each store the user may read",
    )  # fmt: skip
    async def part_detail(
        who: Annotated[Caller, Depends(caller)],
        part_number: Annotated[str, Query(pattern=PART_PATTERN)],
    ) -> PartDetail:
        part = await call_as("inventory", "GetPart", inv.GetPartRequest(part_number=part_number),
                             who)  # fmt: skip
        stock = await call_as("inventory", "GetStock",
                              inv.GetStockRequest(part_number=part_number), who)  # fmt: skip
        return PartDetail(part=_part(part.part), stock=[_stock(s) for s in stock.stock])
