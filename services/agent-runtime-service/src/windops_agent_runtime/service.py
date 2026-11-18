"""The gRPC API: queue a run, read runs, cancel one. Only the Investigation Service may call it.

The Investigation Service owns cases, and decides for each caller whether they may start, read or
cancel a case's runs (SRS 5.5). So this service checks the caller's certificate, not a permission:
only the names in `callers` are let in. The tenant always comes from the verified identity.
"""

import hashlib
import re
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.agent_runtime.v1 import agent_runtime_pb2 as pb
from windops.agent_runtime.v1 import agent_runtime_pb2_grpc
from windops_auth import current_peer_service
from windops_claude import Policy, load
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    current_identity,
)
from windops_migrations import in_tenant

from windops_agent_runtime.queue import new_run_id
from windops_agent_runtime.store import (
    ACTOR,
    CANCELLED,
    QUEUED,
    RUN_CALLS,
    RUN_STEPS,
    RUNS,
    TERMINAL,
    audit,
)
from windops_agent_runtime.workflows import Workflow, canonical

INVESTIGATION = "windops-investigation"
_CASE = re.compile(r"case_[a-z0-9]{20}")
_SNAPSHOT = re.compile(r"evs_[a-z0-9]{20}")
_RUN = re.compile(r"run_[a-z0-9]{20}")
_ACTOR = re.compile(r"(user|service):[A-Za-z0-9._@-]{1,120}")
_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")
MAXIMUM_REASON = 500
DEFAULT_LIMIT, MAXIMUM_LIMIT = 20, 100


def _stamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def _actor(value: str) -> tuple[str, str]:
    if not _ACTOR.fullmatch(value):
        raise InvalidRequestError("requested_by must be user:<id> or service:<name>")
    return value.split(":", 1)[0], value


def _money(value: Any) -> str:
    return f"{Decimal(str(value)):.6f}"


def usage_message(row: Any, used: Any) -> pb.Usage:
    return pb.Usage(
        calls=int(used.calls), tokens=int(used.tokens), cache_read_tokens=int(used.cache_read),
        cost_usd=_money(used.cost), fallbacks=int(used.fallbacks),
        token_budget=int(row.token_budget), cost_budget_usd=_money(row.cost_budget_usd),
    )  # fmt: skip


def run_message(row: Any, steps: list[Any], used: Any = None) -> pb.Run:
    return pb.Run(
        policy_version=row.policy_version, policy_hash=row.policy_hash,
        usage=usage_message(row, used) if used is not None else None,
        run_id=row.run_id, case_id=row.case_id, snapshot_id=row.snapshot_id,
        workflow=row.workflow, workflow_version=row.workflow_version, status=row.status,
        reason=row.reason, attempt=row.attempt, max_attempts=row.max_attempts,
        requested_by=row.requested_by, created_at=_stamp(row.created_at),
        started_at=_stamp(row.started_at), finished_at=_stamp(row.finished_at),
        cancel_requested=row.cancel_requested_at is not None, total_steps=row.total_steps,
        steps=[
            pb.Step(position=s.position, name=s.name, attempt=s.attempt,
                    started_at=_stamp(s.started_at), finished_at=_stamp(s.finished_at))
            for s in steps
        ],
    )  # fmt: skip


class AgentRuntimeService(agent_runtime_pb2_grpc.AgentRuntimeServiceServicer):
    def __init__(
        self,
        engine: AsyncEngine,
        workflows: Mapping[str, Workflow],
        *,
        max_attempts: int = 3,
        policy: Policy | None = None,
        callers: Collection[str] = (INVESTIGATION,),
        peer: Callable[[], str | None] = current_peer_service,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine, self._workflows = engine, workflows
        self._max_attempts, self._callers, self._peer = max_attempts, frozenset(callers), peer
        self._clock = clock
        self._policy = policy or load()

    def _tenant(self) -> str:
        if self._peer() not in self._callers:
            raise PermissionDeniedError("Only the Investigation Service may call this service")
        return current_identity().tenant_id

    async def _load(self, connection: AsyncConnection, tenant: str, run_id: str) -> pb.Run:
        r, s = RUNS.c, RUN_STEPS.c
        row = (
            await connection.execute(select(RUNS).where(r.tenant_id == tenant, r.run_id == run_id))
        ).one_or_none()
        if row is None:
            raise NotFoundError("No such run")
        steps = (
            await connection.execute(
                select(RUN_STEPS)
                .where(s.tenant_id == tenant, s.run_id == run_id)
                .order_by(s.position)
            )
        ).all()
        c = RUN_CALLS.c
        used = (
            await connection.execute(
                select(
                    func.count().label("calls"),
                    func.coalesce(func.sum(c.processed_tokens), 0).label("tokens"),
                    func.coalesce(func.sum(c.cache_read_tokens), 0).label("cache_read"),
                    func.coalesce(func.sum(c.cost_usd), 0).label("cost"),
                    func.coalesce(func.sum(case((c.fallback, 1), else_=0)), 0).label("fallbacks"),
                ).where(c.tenant_id == tenant, c.run_id == run_id)
            )
        ).one()
        return run_message(row, list(steps), used)

    async def StartRun(  # noqa: N802  # The name comes from the contract.
        self, request: pb.StartRunRequest, context: Any
    ) -> pb.StartRunResponse:
        tenant = self._tenant()
        if not _CASE.fullmatch(request.case_id):
            raise InvalidRequestError("case_id must be a case's ID")
        if not _SNAPSHOT.fullmatch(request.snapshot_id):
            raise InvalidRequestError("snapshot_id must be an evidence snapshot's ID")
        if not _KEY.fullmatch(request.idempotency_key):
            raise InvalidRequestError("idempotency_key must be 8 to 128 letters, digits or ._:-")
        actor_type, requested_by = _actor(request.requested_by)
        workflow = self._workflows.get(request.workflow)
        if workflow is None:
            raise InvalidRequestError(f"No workflow named {request.workflow!r}")
        parameters = workflow.check(dict(request.parameters))
        token_budget, cost_budget = self._budget(tenant, request)
        request_hash = hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
        run_id, now = new_run_id(), self._clock()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                existing = await self._by_key(connection, tenant, request.idempotency_key)
                if existing is not None:
                    return await self._repeat(connection, tenant, existing, request_hash)
                await connection.execute(
                    RUNS.insert().values(
                        tenant_id=tenant, run_id=run_id, case_id=request.case_id,
                        snapshot_id=request.snapshot_id, workflow=workflow.name,
                        workflow_version=workflow.version, parameters=canonical(parameters),
                        total_steps=workflow.total_steps(parameters), status=QUEUED,
                        attempt=0, max_attempts=self._max_attempts, requested_by=requested_by,
                        idempotency_key=request.idempotency_key, request_hash=request_hash,
                        created_at=now, not_before=now, updated_at=now,
                        policy_version=self._policy.version,
                        policy_hash=self._policy.content_hash, token_budget=token_budget,
                        cost_budget_usd=cost_budget,
                    )
                )  # fmt: skip
                await audit(
                    connection, tenant_id=tenant, actor_type=actor_type, actor_id=requested_by,
                    action="agent_run.queued", run_id=run_id,
                )  # fmt: skip
                return pb.StartRunResponse(run=await self._load(connection, tenant, run_id))
        except IntegrityError:
            # The same key, in a call that raced this one: answer as a retry.
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                existing = await self._by_key(connection, tenant, request.idempotency_key)
                if existing is None:
                    raise
                return await self._repeat(connection, tenant, existing, request_hash)

    def _budget(self, tenant: str, request: pb.StartRunRequest) -> tuple[int, Decimal]:
        """The tenant's budget (LLM-010), lowered if the request asks. It is never raised."""
        budget = self._policy.budget_for(tenant)
        tokens, cost = budget.tokens, budget.cost_usd
        if request.token_budget < 0:
            raise InvalidRequestError("token_budget cannot be negative")
        if request.token_budget:
            tokens = min(tokens, request.token_budget)
        if request.cost_budget_usd:
            try:
                asked = Decimal(request.cost_budget_usd)
            except InvalidOperation as error:
                raise InvalidRequestError("cost_budget_usd must be a decimal number") from error
            if not asked.is_finite() or asked <= 0:
                raise InvalidRequestError("cost_budget_usd must be above zero")
            cost = min(cost, asked)
        return tokens, cost

    @staticmethod
    async def _by_key(connection: AsyncConnection, tenant: str, key: str) -> Any | None:
        r = RUNS.c
        return (
            await connection.execute(
                select(r.run_id, r.request_hash).where(
                    r.tenant_id == tenant, r.idempotency_key == key
                )
            )
        ).one_or_none()

    async def _repeat(
        self, connection: AsyncConnection, tenant: str, existing: Any, request_hash: str
    ) -> pb.StartRunResponse:
        if existing.request_hash != request_hash:
            raise ConflictError("That idempotency_key was used for a different run")
        return pb.StartRunResponse(run=await self._load(connection, tenant, existing.run_id))

    async def GetRun(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetRunRequest, context: Any
    ) -> pb.GetRunResponse:
        tenant = self._tenant()
        if not _RUN.fullmatch(request.run_id):
            raise InvalidRequestError("run_id must be a run's ID")
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            return pb.GetRunResponse(run=await self._load(connection, tenant, request.run_id))

    async def ListRuns(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListRunsRequest, context: Any
    ) -> pb.ListRunsResponse:
        tenant = self._tenant()
        if not _CASE.fullmatch(request.case_id):
            raise InvalidRequestError("case_id must be a case's ID")
        limit = request.limit or DEFAULT_LIMIT
        if not 1 <= limit <= MAXIMUM_LIMIT:
            raise InvalidRequestError(f"limit must be from 1 to {MAXIMUM_LIMIT}")
        r = RUNS.c
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            ids = (
                await connection.execute(
                    select(r.run_id)
                    .where(r.tenant_id == tenant, r.case_id == request.case_id)
                    .order_by(r.created_at.desc(), r.run_id.desc())
                    .limit(limit)
                )
            ).scalars().all()  # fmt: skip
            return pb.ListRunsResponse(
                runs=[await self._load(connection, tenant, run_id) for run_id in ids]
            )

    async def CancelRun(  # noqa: N802  # The name comes from the contract.
        self, request: pb.CancelRunRequest, context: Any
    ) -> pb.CancelRunResponse:
        tenant = self._tenant()
        if not _RUN.fullmatch(request.run_id):
            raise InvalidRequestError("run_id must be a run's ID")
        actor_type, by = _actor(request.requested_by)
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_REASON:
            raise InvalidRequestError(f"A reason of 1 to {MAXIMUM_REASON} characters is needed")
        r = RUNS.c
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = (
                await connection.execute(
                    select(RUNS)
                    .where(r.tenant_id == tenant, r.run_id == request.run_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("No such run")
            if row.status in TERMINAL or row.cancel_requested_at is not None:
                # Already ended, or already asked: nothing changes, so a retry is harmless.
                return pb.CancelRunResponse(run=await self._load(connection, tenant, row.run_id))
            now = self._clock()
            # A queued run ends now. A running one ends at its worker's next heartbeat.
            ending = (
                {"status": CANCELLED, "reason": reason, "finished_at": now}
                if row.status == QUEUED
                else {}
            )
            await connection.execute(
                update(RUNS)
                .where(r.tenant_id == tenant, r.run_id == row.run_id)
                .values(
                    cancel_requested_at=now, cancel_requested_by=by, cancel_reason=reason,
                    updated_at=now, **ending,
                )
            )  # fmt: skip
            await audit(
                connection, tenant_id=tenant, actor_type=actor_type, actor_id=by,
                action="agent_run.cancel_requested", run_id=row.run_id,
            )  # fmt: skip
            if ending:
                await audit(
                    connection, tenant_id=tenant, actor_type="service",
                    actor_id=ACTOR, action=f"agent_run.{CANCELLED}",
                    run_id=row.run_id,
                )  # fmt: skip
            return pb.CancelRunResponse(run=await self._load(connection, tenant, row.run_id))
