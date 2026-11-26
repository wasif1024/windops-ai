"""The gRPC API. Only a person decides, and only on what is still current.

No agent and no service ever decides (FR-APR-001, SEC-009). The Authorization Service never
grants an agent maintenance.approve; this service refuses one too, before asking, so no change to
the permission table could ever let an agent approve.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.approval.v1 import approval_pb2 as pb
from windops.approval.v1 import approval_pb2_grpc
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnavailableError,
    current_identity,
)
from windops_migrations import in_tenant

from windops_approval.recheck import APPROVAL_RULES, refusal, requesters
from windops_approval.sources import Authorizer, Eligibility, Sources
from windops_approval.store import (
    APPROVALS,
    APPROVED,
    PENDING,
    REJECTED,
    STATUSES,
    SUPERSEDED,
    KeyReusedError,
    announce,
    audit,
    denied,
    remember,
    remembered,
)

READ, APPROVE = "investigation.read", "maintenance.approve"
DECISIONS = {"approve": APPROVED, "reject": REJECTED}
# Whether the caller may decide a pending request now (M11.4).
DECIDABLE, STALE_STANDING, MOVED_ON = "decidable", "stale", "moved_on"
REQUESTER, NOT_ELIGIBLE, UNKNOWN = "requester", "not_eligible", "unknown"
# How many requests are explained at once, each asking Authorization, Maintenance and
# Investigation as the caller.
EXPLAIN_AT_ONCE = 8
DEFAULT_PAGE, MAXIMUM_PAGE, MAXIMUM_KEY, MAXIMUM_REASON = 50, 200, 64, 1000


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def to_message(row: Any) -> pb.Approval:
    return pb.Approval(
        approval_id=row.approval_id, work_order_id=row.work_order_id,
        proposal_version=row.proposal_version, case_id=row.case_id, turbine_id=row.turbine_id,
        evidence_snapshot_id=row.evidence_snapshot_id, status=row.status,
        submitted_by=row.submitted_by, drafted_by=row.drafted_by,
        drafted_by_agent=row.drafted_by_agent, submitted_at=_timestamp(row.submitted_at),
        requested_at=_timestamp(row.requested_at),
        decision=pb.Decision.FromString(row.decision) if row.decision else None,
        superseded_reason=row.superseded_reason, superseded_at=_timestamp(row.superseded_at),
    )  # fmt: skip


class ApprovalService(approval_pb2_grpc.ApprovalServiceServicer):
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

    async def _person(self) -> None:
        """Only a person, with no agent acting for them, decides (FR-APR-001, SEC-009). The
        refusal is recorded, as Authorization would record its own."""
        identity = current_identity()
        if identity.actor or not identity.user_id:
            async with self._engine.begin() as connection:
                await denied(connection, identity, APPROVE)
            raise PermissionDeniedError(
                "Only a person may decide on a work order: never an agent (FR-APR-001, SEC-009)"
            )

    async def _allowed(self, permission: str, turbine_id: str) -> bool:
        if await self._authorizer.allowed(permission):
            return True
        try:
            farm = str((await self._sources.turbine(turbine_id)).wind_farm_id)
        except NotFoundError:
            return False
        return await self._authorizer.allowed(permission, farm, turbine_id)

    async def _eligible(self, turbine_id: str, since: datetime) -> Eligibility:
        """maintenance.approve, held now, for the tenant or else the turbine's wind farm, and not
        only by the decider's own change since the request (RBAC-005, RBAC-006)."""
        found = await self._authorizer.eligible(APPROVE, since)
        if found.allowed:
            return found
        try:
            farm = str((await self._sources.turbine(turbine_id)).wind_farm_id)
        except NotFoundError:
            return found
        return await self._authorizer.eligible(APPROVE, since, farm, turbine_id)

    async def _require(self, permission: str, turbine_id: str) -> None:
        """One the caller may not read is not found; one they may read but not decide, refused."""
        if not await self._allowed(READ, turbine_id):
            raise NotFoundError("No such approval")
        if permission != READ and not await self._allowed(permission, turbine_id):
            raise PermissionDeniedError("You do not have permission to decide on work orders")

    async def _standing(self, row: Any) -> tuple[str, str]:
        """Whether the caller may decide this pending request now, and if not, why: the checks
        a decision runs, read-only. It never changes the request."""
        identity = current_identity()
        if identity.actor or not identity.user_id:
            return NOT_ELIGIBLE, "Only a person decides on a work order: never an agent"
        submitted = row.submitted_at if row.submitted_at.tzinfo else row.submitted_at.replace(
            tzinfo=UTC)  # fmt: skip
        try:
            eligibility = await self._eligible(row.turbine_id, submitted)
            if not eligibility.allowed:
                return NOT_ELIGIBLE, (
                    eligibility.reason if "RBAC-006" in eligibility.reason
                    else "You may not decide on work orders for this turbine"
                )  # fmt: skip
            answer = await self._sources.work_order(row.work_order_id)
            case = await self._sources.case(row.case_id)
        except (UnavailableError, NotFoundError):
            return UNKNOWN, "It cannot be checked against the current work order right now"
        work_order = answer.work_order
        found = refusal(row, work_order, case)
        if found is not None:
            stale = bool(work_order.stale_rule or case.stale_rule)
            return (STALE_STANDING if stale else MOVED_ON), found.reason
        if identity.user_id in requesters(row, answer.history):
            return REQUESTER, (
                "You drafted, submitted or changed this work order, or an agent drafted it for "
                "you, so another Maintenance Manager must decide it (RBAC-004, RBAC-008)"
            )
        return DECIDABLE, ""

    async def _explained(self, rows: list[Any]) -> list[pb.Approval]:
        """Each request, a pending one with its standing for the caller."""
        gate = asyncio.Semaphore(EXPLAIN_AT_ONCE)

        async def one(row: Any) -> pb.Approval:
            message = to_message(row)
            if row.status == PENDING:
                async with gate:
                    message.standing, message.standing_reason = await self._standing(row)
            return message

        return list(await asyncio.gather(*(one(row) for row in rows)))

    # --- storage

    @staticmethod
    async def _load(connection: AsyncConnection, tenant: str, approval_id: str) -> Any:
        a = APPROVALS.c
        row = (await connection.execute(
            select(APPROVALS).where(a.tenant_id == tenant, a.approval_id == approval_id)
        )).one_or_none()  # fmt: skip
        if row is None:
            raise NotFoundError("No such approval")
        return row

    @staticmethod
    async def _settle(connection: AsyncConnection, row: Any, **values: Any) -> bool:
        """Change a pending request, once. False if someone else settled it first."""
        a = APPROVALS.c
        changed = await connection.execute(
            update(APPROVALS)
            .where(a.tenant_id == row.tenant_id, a.approval_id == row.approval_id,
                   a.status == PENDING)
            .values(**values)
        )  # fmt: skip
        return bool(changed.rowcount)

    # --- the API

    async def GetApproval(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetApprovalRequest, context: Any
    ) -> pb.GetApprovalResponse:
        tenant = current_identity().tenant_id
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.approval_id)
        await self._require(READ, row.turbine_id)
        if request.explain:
            return pb.GetApprovalResponse(approval=(await self._explained([row]))[0])
        return pb.GetApprovalResponse(approval=to_message(row))

    async def ListApprovals(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListApprovalsRequest, context: Any
    ) -> pb.ListApprovalsResponse:
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
        a = APPROVALS.c
        query = select(APPROVALS).where(a.tenant_id == tenant)
        for column, value in (
            (a.work_order_id, request.work_order_id),
            (a.case_id, request.case_id),
            (a.turbine_id, request.turbine_id),
        ):
            if value:
                query = query.where(column == value)
        if request.statuses:
            query = query.where(a.status.in_(list(request.statuses)))
        offset = int(request.page_token) if request.page_token.isdigit() else 0
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(
                query.order_by(a.requested_at, a.approval_id).offset(offset).limit(size + 1)
            )).all()  # fmt: skip
        page = list(rows[:size])
        return pb.ListApprovalsResponse(
            approvals=await self._explained(page)
            if request.explain
            else [to_message(r) for r in page],
            next_page_token=str(offset + size) if len(rows) > size else "",
        )

    async def DecideApproval(  # noqa: N802  # The name comes from the contract.
        self, request: pb.DecideApprovalRequest, context: Any
    ) -> pb.DecideApprovalResponse:
        # Before anything else: an agent never decides, whatever it was granted (FR-APR-001).
        await self._person()
        identity = current_identity()
        tenant, who = identity.tenant_id, str(identity.user_id)
        outcome = DECISIONS.get(request.decision)
        reason = request.reason.strip()
        if outcome is None:
            raise InvalidRequestError("decision is approve or reject")
        if len(reason) > MAXIMUM_REASON or (outcome == REJECTED and not reason):
            raise InvalidRequestError(
                f"A reason of at most {MAXIMUM_REASON} characters; a rejection needs one"
            )
        if not request.expected_proposal_version or not request.expected_evidence_snapshot_id:
            raise InvalidRequestError("Name the proposal version and snapshot you reviewed")
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, who, request.idempotency_key,
                                         "DecideApproval", request)  # fmt: skip
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.DecideApprovalResponse.FromString(first)
            row = await self._load(connection, tenant, request.approval_id)
        await self._require(READ, row.turbine_id)
        submitted = row.submitted_at if row.submitted_at.tzinfo else row.submitted_at.replace(
            tzinfo=UTC)  # fmt: skip
        eligibility = await self._eligible(row.turbine_id, submitted)
        if not eligibility.allowed:
            raise PermissionDeniedError(
                "You may not decide on this request: " + eligibility.reason
                if "RBAC-006" in eligibility.reason
                else "You do not have permission to decide on work orders"
            )
        if row.status != PENDING:
            raise ConflictError(f"It is already {row.status}, and cannot be decided again")
        if (
            request.expected_proposal_version != row.proposal_version
            or request.expected_evidence_snapshot_id != row.evidence_snapshot_id
        ):
            raise ConflictError(
                f"This request is for version {row.proposal_version} on snapshot "
                f"{row.evidence_snapshot_id}, not what you reviewed: read it again"
            )  # fmt: skip
        # EVD-009: what is current now, read as the decider. Anything unreadable refuses the
        # decision, and changes nothing (EVD-010).
        try:
            answer = await self._sources.work_order(row.work_order_id)
            case = await self._sources.case(row.case_id)
        except (UnavailableError, NotFoundError) as error:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                await audit(connection, identity, action="approval.refused",
                            resource_type="approval", resource_id=row.approval_id,
                            outcome="failed")  # fmt: skip
            raise UnavailableError(
                "The decision cannot be checked against the current work order and evidence "
                "right now, so it is refused: try again later"
            ) from error
        work_order = answer.work_order
        found = refusal(row, work_order, case)
        # RBAC-004, RBAC-008: from the work order's own history, read now.
        excluded = requesters(row, answer.history)
        own = None if found is not None or who not in excluded else (
            "You drafted, submitted or changed this work order, or an agent drafted it for you, "
            "so another Maintenance Manager must decide it (RBAC-004, RBAC-008)")  # fmt: skip
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            if own is not None:
                await audit(connection, identity, action="approval.refused",
                            resource_type="approval", resource_id=row.approval_id,
                            outcome="denied")  # fmt: skip
            elif found is not None:
                if found.supersedes:
                    await self._settle(
                        connection,
                        row,
                        status=SUPERSEDED,
                        superseded_reason=found.reason,
                        superseded_at=now,
                    )
                await audit(connection, identity, action="approval.refused",
                            resource_type="approval", resource_id=row.approval_id,
                            outcome="denied")  # fmt: skip
            else:
                validation = work_order.validation
                decision = pb.Decision(
                    outcome=outcome, decided_by=who, decided_at=_timestamp(now), reason=reason,
                    proposal_version=row.proposal_version,
                    evidence_snapshot_id=row.evidence_snapshot_id,
                    policy=pb.PolicyContext(
                        policy_result_id=validation.policy_result_id,
                        policy_outcome=validation.policy_outcome,
                        validated_at=validation.checked_at, case_status=case.status,
                        evidence_version=case.evidence_version,
                        work_order_revision=work_order.revision, approval_rules=APPROVAL_RULES,
                        permissions_version=eligibility.permissions_version,
                    ),
                    requesters=excluded,
                )  # fmt: skip
                if not await self._settle(connection, row, status=outcome, decided_by=who,
                                          decided_at=now,
                                          decision=decision.SerializeToString()):  # fmt: skip
                    raise ConflictError("Someone else settled it first: read it again")
                await announce(connection, tenant, row, decision)
                await audit(connection, identity, action=f"approval.{outcome}",
                            resource_type="approval", resource_id=row.approval_id,
                            outcome="succeeded")  # fmt: skip
                response = pb.DecideApprovalResponse(
                    approval=to_message(await self._load(connection, tenant, row.approval_id))
                )
                await remember(connection, tenant_id=tenant, caller=who,
                               key=request.idempotency_key, method="DecideApproval",
                               request=request, response=response, now=now)  # fmt: skip
                # Returned only once the decision and its event are committed (EVT-004).
        if own is not None:
            raise PermissionDeniedError(own)
        if found is not None:
            raise ConflictError(found.reason)
        return response
