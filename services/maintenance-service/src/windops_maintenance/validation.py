"""A work order, validated now (FR-MNT-002): its plan, its turbine, its parts, and the Policy
Service's rules.

Each source is asked as the caller, so it decides for them. What cannot be asked is a blocking
problem: a submission never goes ahead on a check that did not run (SRS 7.7).
"""

from datetime import datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from windops.asset.v1 import asset_pb2
from windops.authorization.v1 import authorization_pb2
from windops.inventory.v1 import inventory_pb2
from windops.investigation.v1 import investigation_pb2
from windops.maintenance.v1 import maintenance_pb2 as pb
from windops.policy.v1 import policy_pb2
from windops_grpc import NotFoundError, ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

from windops_maintenance import plans

DEADLINE_SECONDS = 3.0
# The Policy Service reads the case, its evidence, the turbine, the estimate and a forecast.
POLICY_DEADLINE_SECONDS = 15.0
# A turbine in these is noted for the reviewer; it does not stop the plan.
NOTED_STATUSES = ("fault", "maintenance", "offline", "stopped")
# What the Inventory Service found, and whether it stops submission. A shortage is for the
# reviewer: the parts may be ordered or moved before the window. Naming a part reserves nothing.
PART_PROBLEMS = {
    "unknown": (True, "No such part in the catalog"),
    "retired": (True, "The part is retired"),
    "incompatible": (True, "It does not fit this turbine's model"),
    "short": (False, "Not enough in the wind farm's store"),
}


async def ask(
    client: ServiceClient,
    method: Any,
    request: Message,
    who: str,
    deadline: float = DEADLINE_SECONDS,
) -> Any:
    try:
        return await client.call(method, request, idempotent=True, deadline_seconds=deadline)
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError(f"{who} has no such record for this caller") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...


class Sources(Protocol):
    """The owners this service asks, each as the caller."""

    async def case(self, case_id: str) -> Any: ...
    async def turbine(self, turbine_id: str) -> Any: ...
    async def parts(self, turbine: Any, lines: list[pb.PartLine]) -> list[Any]: ...
    async def evaluate(
        self, case_id: str, proposal: policy_pb2.Proposal, impact_result_id: str, key: str
    ) -> Any: ...


class AuthorizationClient:
    """Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        try:
            answer = await ask(self._client, self._stub().Check, request, "Authorization")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)


class ServiceSources:
    def __init__(self, stubs: dict[str, Any], clients: dict[str, ServiceClient]) -> None:
        self._stubs, self._clients = stubs, clients

    async def case(self, case_id: str) -> Any:
        request = investigation_pb2.GetCaseRequest(case_id=case_id)
        return (
            await ask(
                self._clients["investigation"],
                self._stubs["investigation"]().GetCase,
                request,
                "The Investigation Service",
            )
        ).case

    async def turbine(self, turbine_id: str) -> Any:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        return (
            await ask(
                self._clients["asset"],
                self._stubs["asset"]().GetTurbine,
                request,
                "The Asset Service",
            )
        ).turbine

    async def parts(self, turbine: Any, lines: list[pb.PartLine]) -> list[Any]:
        request = inventory_pb2.CheckPartsRequest(
            model=inventory_pb2.ModelRef(
                manufacturer=turbine.model.manufacturer, model=turbine.model.model
            ),
            wind_farm_id=turbine.wind_farm_id,
            lines=[
                inventory_pb2.PartLine(part_number=line.part_number, quantity=line.quantity)
                for line in lines
            ],
        )
        return list(
            (
                await ask(
                    self._clients["inventory"],
                    self._stubs["inventory"]().CheckParts,
                    request,
                    "The Inventory Service",
                )
            ).checks
        )

    async def evaluate(
        self, case_id: str, proposal: policy_pb2.Proposal, impact_result_id: str, key: str
    ) -> Any:
        request = policy_pb2.EvaluateProposalRequest(
            case_id=case_id,
            proposal=proposal,
            impact_result_id=impact_result_id,
            idempotency_key=key,
        )
        return (
            await ask(
                self._clients["policy"],
                self._stubs["policy"]().EvaluateProposal,
                request,
                "The Policy Service",
                POLICY_DEADLINE_SECONDS,
            )
        ).result


def _stamp(value: datetime) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


async def validate(
    sources: Sources, case: Any, plan: pb.Plan, *, key: str, now: datetime
) -> pb.Validation:
    """Every check, with what it found. Passed when nothing blocks."""
    found = plans.check(plan, now)
    if getattr(case, "stale_rule", ""):
        # A stale recommendation cannot go forward until the case is re-evaluated (EVD-007).
        found.append(
            pb.Problem(
                field="evidence",
                code="stale_evidence",
                blocking=True,
                message=f"The case's evidence is stale ({case.stale_rule}: {case.stale_detail}); "
                "re-evaluate the case, then revise this work order",
            )
        )
    status, turbine = "", None
    try:
        turbine = await sources.turbine(case.turbine_id)
        status = str(turbine.status)
    except NotFoundError:
        found.append(
            pb.Problem(
                field="turbine",
                code="unknown_turbine",
                blocking=True,
                message="The case's turbine cannot be found",
            )
        )
    except UnavailableError:
        found.append(
            pb.Problem(
                field="turbine",
                code="asset_unavailable",
                blocking=True,
                message="The turbine's status cannot be checked right now",
            )
        )
    if status in NOTED_STATUSES:
        found.append(
            pb.Problem(
                field="turbine",
                code=f"turbine_{status}",
                blocking=False,
                message=f"The turbine is {status}",
            )
        )
    available = await _parts(sources, turbine, plan, found)
    outcome = result_id = ""
    if plan.action in plans.ACTIONS:
        proposal = policy_pb2.Proposal(
            action=plan.action,
            summary=plan.summary[:2000],
            requires_climb=plans.needs_climb(plan),
            estimated_downtime_hours=plan.estimated_downtime_hours,
        )
        windowed = plan.HasField("planned_start") and plan.HasField("planned_end")
        if windowed and plan.planned_end.ToDatetime() > plan.planned_start.ToDatetime():
            proposal.planned_start.CopyFrom(plan.planned_start)
            proposal.planned_end.CopyFrom(plan.planned_end)
        try:
            result = await sources.evaluate(case.case_id, proposal, plan.impact_result_id, key)
            outcome, result_id = str(result.outcome), str(result.result_id)
        except (NotFoundError, UnavailableError):
            found.append(
                pb.Problem(
                    field="policy",
                    code="policy_unavailable",
                    blocking=True,
                    message="The Policy Service cannot check it right now",
                )
            )
        if outcome == "block":
            found.append(
                pb.Problem(
                    field="policy",
                    code="policy_block",
                    blocking=True,
                    message="The Policy Service's rules block this work",
                )
            )
        elif outcome == "escalate":
            found.append(
                pb.Problem(
                    field="policy",
                    code="policy_escalate",
                    blocking=False,
                    message="The Policy Service's rules escalate it for review",
                )
            )
    return pb.Validation(
        checked_at=_stamp(now),
        passed=not any(p.blocking for p in found),
        problems=found,
        turbine_status=status,
        policy_outcome=outcome,
        policy_result_id=result_id,
        parts=available,
    )


async def _parts(
    sources: Sources, turbine: Any, plan: pb.Plan, found: list[pb.Problem]
) -> list[pb.PartAvailability]:
    """Whether each part fits the turbine and is in its wind farm's store. If the Inventory
    Service cannot say, no part is known to be available, and submission waits (SRS 7.7)."""
    if not plan.parts or turbine is None or any(p.field.startswith("parts[") for p in found):
        # A malformed or repeated line is already a blocking problem of the plan's own.
        return []
    lines = list(plan.parts)
    try:
        checks = await sources.parts(turbine, lines)
    except (NotFoundError, UnavailableError):
        found.append(
            pb.Problem(
                field="parts",
                code="inventory_unavailable",
                blocking=True,
                message="The parts cannot be checked against inventory right now",
            )
        )
        return []
    for index, check in enumerate(checks):
        if check.status in PART_PROBLEMS:
            blocking, why = PART_PROBLEMS[check.status]
            found.append(
                pb.Problem(
                    field=f"parts[{index}]",
                    code=f"part_{check.status}",
                    blocking=blocking,
                    message=f"{check.part_number}: {check.message or why}",
                )
            )
    return [
        pb.PartAvailability(
            part_number=c.part_number, quantity=c.quantity, status=c.status, available=c.available
        )
        for c in checks
    ]
