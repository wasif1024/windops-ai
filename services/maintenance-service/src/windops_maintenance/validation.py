"""A work order, validated now (FR-MNT-002): its plan, its turbine, and the Policy Service's rules.

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
    status = ""
    try:
        status = str((await sources.turbine(case.turbine_id)).status)
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
    )
