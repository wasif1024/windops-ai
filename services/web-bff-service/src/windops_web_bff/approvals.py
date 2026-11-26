"""The approval queue's API: requests for a decision, and the decision (M10.2, SRS 4.11).

It decides nothing. The Approval Service decides who may decide, rechecks each request against
the current work order and evidence, and refuses any agent (FR-APR-001, RBAC-004 to RBAC-008).
Each call is made as the signed-in user, and a decision is never retried here: the browser
resends it with the same idempotency key.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import grpc
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from windops.approval.v1 import approval_pb2 as apr

from windops_web_bff.common import Call, Key, time_of
from windops_web_bff.errors import problem
from windops_web_bff.tokens import Caller

ApprovalId = Annotated[str, Path(pattern=r"^apr_[a-z0-9]{20}$")]
STATUSES = ("pending", "approved", "rejected", "superseded")
# Refusals the screen explains, by the requirement the Approval Service names in its refusal.
# Only this code goes to the browser, never the service's words.
EXPLAINED = (
    ("(RBAC-004, RBAC-008)", "requester", "You had a hand in this work order"),
    ("(RBAC-006)", "own_change", "Only your own change since the request makes you eligible"),
)


class PolicyContext(BaseModel):
    policy_result_id: str
    # "pass" or "escalate".
    policy_outcome: str
    validated_at: str | None
    case_status: str
    evidence_version: int
    work_order_revision: int
    approval_rules: str
    permissions_version: int


class Decision(BaseModel):
    # "approved" or "rejected".
    outcome: str
    decided_by: str
    decided_at: str | None
    reason: str
    proposal_version: int
    evidence_snapshot_id: str
    policy: PolicyContext
    # Who could not decide it, because they had a hand in it.
    requesters: list[str]


class Approval(BaseModel):
    approval_id: str
    work_order_id: str
    proposal_version: int
    case_id: str
    turbine_id: str
    evidence_snapshot_id: str
    # "pending", "approved", "rejected" or "superseded".
    status: str
    submitted_by: str
    drafted_by: str
    # "agent:maintenance-planner" when an agent drafted it; "" for a person.
    drafted_by_agent: str
    submitted_at: str | None
    requested_at: str | None
    decision: Decision | None
    superseded_reason: str
    superseded_at: str | None
    # For a pending request: whether the user may decide it now (M11.4). "decidable", "stale",
    # "moved_on", "requester", "not_eligible" or "unknown"; empty otherwise.
    standing: str = ""
    standing_reason: str = ""


class ApprovalList(BaseModel):
    approvals: list[Approval]
    next_page_token: str


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=1000)
    # What the user reviewed: a request that no longer matches is refused.
    expected_proposal_version: int = Field(ge=1)
    expected_evidence_snapshot_id: str = Field(pattern=r"^evs_[a-z0-9]{20}$")


def _approval(message: Any) -> Approval:
    decided = None
    if message.HasField("decision"):
        d, p = message.decision, message.decision.policy
        decided = Decision(
            outcome=d.outcome, decided_by=d.decided_by, decided_at=time_of(d, "decided_at"),
            reason=d.reason, proposal_version=d.proposal_version,
            evidence_snapshot_id=d.evidence_snapshot_id, requesters=list(d.requesters),
            policy=PolicyContext(
                policy_result_id=p.policy_result_id, policy_outcome=p.policy_outcome,
                validated_at=time_of(p, "validated_at"), case_status=p.case_status,
                evidence_version=p.evidence_version, work_order_revision=p.work_order_revision,
                approval_rules=p.approval_rules, permissions_version=p.permissions_version,
            ),
        )  # fmt: skip
    return Approval(
        approval_id=message.approval_id, work_order_id=message.work_order_id,
        proposal_version=message.proposal_version, case_id=message.case_id,
        turbine_id=message.turbine_id, evidence_snapshot_id=message.evidence_snapshot_id,
        status=message.status, submitted_by=message.submitted_by, drafted_by=message.drafted_by,
        drafted_by_agent=message.drafted_by_agent,
        submitted_at=time_of(message, "submitted_at"),
        requested_at=time_of(message, "requested_at"), decision=decided,
        superseded_reason=message.superseded_reason,
        superseded_at=time_of(message, "superseded_at"), standing=message.standing,
        standing_reason=message.standing_reason,
    )  # fmt: skip


def add_approval_routes(
    router: APIRouter, caller: Callable[..., Awaitable[Caller]], call_as: Call, fail: Any
) -> None:
    """`fail` turns a problem into a raisable error."""

    @router.get(
        "/approvals", response_model=ApprovalList,
        summary="Approval requests the user may read, oldest first, by status, work order or case",
    )  # fmt: skip
    async def list_approvals(
        who: Annotated[Caller, Depends(caller)],
        status: Annotated[list[str] | None, Query(max_length=4)] = None,
        work_order_id: Annotated[str, Query(max_length=64)] = "",
        case_id: Annotated[str, Query(max_length=64)] = "",
        turbine_id: Annotated[str, Query(max_length=64)] = "",
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(max_length=64)] = "",
    ) -> ApprovalList:
        request = apr.ListApprovalsRequest(
            statuses=[s for s in status or [] if s in STATUSES], work_order_id=work_order_id,
            case_id=case_id, turbine_id=turbine_id, page_size=page_size, page_token=page_token,
            # Each pending request says whether this user may decide it, and if not, why.
            explain=True,
        )  # fmt: skip
        answer = await call_as("approval", "ListApprovals", request, who)
        return ApprovalList(approvals=[_approval(a) for a in answer.approvals],
                            next_page_token=answer.next_page_token)  # fmt: skip

    @router.get(
        "/approvals/{approval_id}", response_model=Approval,
        summary="One approval request, with its decision once made",
    )  # fmt: skip
    async def get_approval(
        approval_id: ApprovalId, who: Annotated[Caller, Depends(caller)]
    ) -> Approval:
        answer = await call_as(
            "approval",
            "GetApproval",
            apr.GetApprovalRequest(approval_id=approval_id, explain=True),
            who,
        )
        return _approval(answer.approval)

    @router.post(
        "/approvals/{approval_id}/decision", response_model=Approval,
        summary="Approve or reject a pending request, rechecked against the current work order "
                "and evidence. Only a person, never one who had a hand in it",
    )  # fmt: skip
    async def decide(
        approval_id: ApprovalId, body: DecisionIn, key: Key,
        who: Annotated[Caller, Depends(caller)],
    ) -> Approval:  # fmt: skip
        request = apr.DecideApprovalRequest(
            approval_id=approval_id, decision=body.decision, reason=body.reason,
            expected_proposal_version=body.expected_proposal_version,
            expected_evidence_snapshot_id=body.expected_evidence_snapshot_id,
            idempotency_key=key,
        )  # fmt: skip
        try:
            answer = await call_as("approval", "DecideApproval", request, who, retry=False)
        except Exception as error:
            cause = error.__cause__
            if isinstance(cause, grpc.aio.AioRpcError) and (
                cause.code() is grpc.StatusCode.PERMISSION_DENIED
            ):
                for marker, code, title in EXPLAINED:
                    if marker in (cause.details() or ""):
                        raise fail(problem(403, title, code)) from error
            raise
        return _approval(answer.approval)
