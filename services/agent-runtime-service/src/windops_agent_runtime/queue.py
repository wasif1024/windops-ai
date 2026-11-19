"""The run queue, in PostgreSQL (ARC-007, ADR 0016).

    StartRun ──> queued ──claim──> running ──> completed | failed | cancelled | stale
                   ^                  │
                   └── lease expired, a failed attempt, or a worker shutting down

A worker claims the oldest run that is due, with FOR UPDATE SKIP LOCKED, so two replicas never
take the same one. The claim is a lease: the worker renews it while it works, and every write it
makes names the claim's token. A worker that stops, however it stops, stops renewing, so its lease
runs out and another replica claims the run, as its next attempt. Each claim is an attempt, and a
run that has used them all fails instead of being claimed again (FR-INV-007). A worker shutting
down hands its run back at once, and that does not count as an attempt.

Claiming works across tenants, as only platform work may. Everything else names the tenant.
"""

import contextlib
import json
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, case, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_claude import Call, Spent
from windops_migrations import across_tenants, in_tenant

if TYPE_CHECKING:
    from windops_agent_runtime.team.sandbox import SandboxUse

from windops_agent_runtime.store import (
    ACTOR,
    CANCELLED,
    FAILED,
    PAUSED,
    QUEUED,
    RUN_CALLS,
    RUN_STEPS,
    RUNNING,
    RUNS,
    STALE,
    TERMINAL,
    audit,
)

# Claims looked at in one go, so expired or cancelled runs ahead in the queue cannot block it.
_SWEEP = 10


class LeaseLostError(Exception):
    """The worker's claim is no longer the run's claim. Another replica has the run."""


@dataclass(frozen=True)
class Claim:
    tenant_id: str
    run_id: str
    token: str
    owner: str
    attempt: int
    max_attempts: int
    workflow: str
    workflow_version: str
    parameters: dict[str, Any]
    # Completed steps, by position, from earlier attempts.
    completed: frozenset[int]
    reclaimed: bool
    # The Claude policy and budget it was queued with (AGT-004, LLM-010).
    policy_version: int = 0
    policy_hash: str = ""
    token_budget: int = 0
    cost_budget_usd: Decimal = Decimal(0)
    # The graph, prompt, tool and skill versions it was queued with (AGT-004).
    pins: dict[str, Any] | None = None
    case_id: str = ""
    snapshot_id: str = ""
    delegation_id: str = ""
    requested_by: str = ""


@dataclass(frozen=True)
class Renewal:
    held: bool
    cancel_requested: bool
    pause_requested: bool = False


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def new_run_id() -> str:
    return "run_" + "".join(
        secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(20)
    )


class Queue:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lease_seconds: float,
        queue_limit: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine, self._lease, self._queue_limit = engine, lease_seconds, queue_limit
        self._clock = clock

    @property
    def lease_seconds(self) -> float:
        return self._lease

    async def claim(self, owner: str) -> Claim | None:
        """Take the oldest due run, or return None. Runs found cancelled, too old, or out of
        attempts are ended on the way."""
        async with self._engine.begin() as connection:
            await across_tenants(connection)
            now = self._clock()
            r = RUNS.c
            due = or_(
                and_(r.status == QUEUED, r.not_before <= now),
                and_(r.status == RUNNING, r.lease_expires_at < now),
            )
            rows = (
                await connection.execute(
                    select(RUNS).where(due).order_by(r.created_at, r.run_id).limit(_SWEEP)
                    .with_for_update(skip_locked=True)
                )
            ).all()  # fmt: skip
            for row in rows:
                ended = self._ending(row, now)
                if ended is not None:
                    status, reason = ended
                    await _end(connection, row, status=status, reason=reason, now=now)
                    continue
                if row.pause_requested_at is not None:
                    # Its worker stopped before it could pause it: pause it now, as asked.
                    await connection.execute(
                        update(RUNS)
                        .where(r.tenant_id == row.tenant_id, r.run_id == row.run_id)
                        .values(status=PAUSED, lease_token="", lease_owner="",
                                lease_expires_at=None, updated_at=now)
                    )  # fmt: skip
                    continue
                token = secrets.token_hex(16)
                await connection.execute(
                    update(RUNS)
                    .where(r.tenant_id == row.tenant_id, r.run_id == row.run_id)
                    .values(
                        status=RUNNING, attempt=row.attempt + 1, lease_token=token,
                        lease_owner=owner, lease_expires_at=now + timedelta(seconds=self._lease),
                        started_at=func.coalesce(r.started_at, now), updated_at=now,
                    )
                )  # fmt: skip
                s = RUN_STEPS.c
                done = (
                    await connection.execute(
                        select(s.position).where(
                            s.tenant_id == row.tenant_id, s.run_id == row.run_id
                        )
                    )
                ).scalars()
                return Claim(
                    tenant_id=row.tenant_id, run_id=row.run_id, token=token, owner=owner,
                    attempt=row.attempt + 1, max_attempts=row.max_attempts,
                    workflow=row.workflow, workflow_version=row.workflow_version,
                    parameters=json.loads(row.parameters), completed=frozenset(done),
                    reclaimed=row.status == RUNNING, policy_version=row.policy_version,
                    policy_hash=row.policy_hash, token_budget=row.token_budget,
                    cost_budget_usd=Decimal(str(row.cost_budget_usd)),
                    pins=json.loads(row.pins or "{}"), case_id=row.case_id,
                    snapshot_id=row.snapshot_id, delegation_id=row.delegation_id,
                    requested_by=row.requested_by,
                )  # fmt: skip
        return None

    def _ending(self, row: Any, now: datetime) -> tuple[str, str] | None:
        if row.cancel_requested_at is not None:
            return CANCELLED, row.cancel_reason or "Cancelled"
        if row.status == QUEUED and row.started_at is None:
            created = _utc(row.created_at)
            if created is not None and created < now - self._queue_limit:
                return STALE, "Waited in the queue longer than allowed"
        if row.attempt >= row.max_attempts:
            return FAILED, f"Stopped after {row.attempt} attempts"
        return None

    async def renew(self, claim: Claim) -> Renewal:
        """Extend the lease. Not held means another replica has the run: stop at once."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            now = self._clock()
            r = RUNS.c
            result = await connection.execute(
                update(RUNS)
                .where(*self._mine(claim))
                .values(lease_expires_at=now + timedelta(seconds=self._lease), updated_at=now)
            )
            if result.rowcount != 1:
                return Renewal(held=False, cancel_requested=False)
            cancel, pause = (
                await connection.execute(
                    select(r.cancel_requested_at, r.pause_requested_at).where(
                        r.tenant_id == claim.tenant_id, r.run_id == claim.run_id
                    )
                )
            ).one()
            return Renewal(
                held=True, cancel_requested=cancel is not None, pause_requested=pause is not None
            )

    async def complete_step(
        self, claim: Claim, *, position: int, name: str, started_at: datetime
    ) -> None:
        """Record a completed step, only while the claim is still the run's."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            now = self._clock()
            # Takes the row's lock, so the step and the lease check are one decision.
            result = await connection.execute(
                update(RUNS).where(*self._mine(claim)).values(updated_at=now)
            )
            if result.rowcount != 1:
                raise LeaseLostError(claim.run_id)
            await connection.execute(
                insert(RUN_STEPS).values(
                    tenant_id=claim.tenant_id, run_id=claim.run_id, position=position, name=name,
                    attempt=claim.attempt, worker=claim.owner, started_at=started_at,
                    finished_at=now,
                )
            )  # fmt: skip

    @contextlib.asynccontextmanager
    async def reading(self, claim: Claim) -> AsyncIterator[AsyncConnection]:
        """A transaction in the run's tenant, for reads."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            yield connection

    @contextlib.asynccontextmanager
    async def holding(self, claim: Claim) -> AsyncIterator[AsyncConnection]:
        """A transaction that writes only while the claim is still the run's. The lease check
        takes the run's row lock, so the check and the writes are one decision."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            result = await connection.execute(
                update(RUNS).where(*self._mine(claim)).values(updated_at=self._clock())
            )
            if result.rowcount != 1:
                raise LeaseLostError(claim.run_id)
            yield connection

    async def spent(self, claim: Claim) -> Spent:
        """What the run has used, over every attempt."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            return await _spent(connection, claim)

    async def record_call(self, claim: Claim, call: Call) -> Spent:
        """Record a call to Claude, only while the claim is still the run's, and return what the
        run has used. A worker that lost its lease records nothing, and is stopped."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            now = self._clock()
            result = await connection.execute(
                update(RUNS).where(*self._mine(claim)).values(updated_at=now)
            )
            if result.rowcount != 1:
                raise LeaseLostError(claim.run_id)
            c = RUN_CALLS.c
            position = (
                await connection.execute(
                    select(func.coalesce(func.max(c.position), 0)).where(
                        c.tenant_id == claim.tenant_id, c.run_id == claim.run_id
                    )
                )
            ).scalar_one() + 1
            usage = call.usage
            await connection.execute(
                insert(RUN_CALLS).values(
                    tenant_id=claim.tenant_id, run_id=claim.run_id, position=position,
                    attempt=claim.attempt, worker=claim.owner, role=call.role,
                    requested_model=call.requested_model, served_model=call.served_model,
                    effort=call.effort, stop_reason=call.stop_reason, fallback=call.fallback,
                    input_tokens=usage.input_tokens,
                    cache_write_5m_tokens=usage.cache_write_5m_tokens,
                    cache_write_1h_tokens=usage.cache_write_1h_tokens,
                    cache_read_tokens=usage.cache_read_tokens, output_tokens=usage.output_tokens,
                    processed_tokens=usage.processed_tokens, cost_usd=call.cost_usd, at=now,
                )
            )  # fmt: skip
            return await _spent(connection, claim)

    async def models_used(self, claim: Claim) -> dict[str, list[str]]:
        c = RUN_CALLS.c
        async with self.reading(claim) as connection:
            rows = (
                await connection.execute(
                    select(c.role, c.served_model)
                    .where(c.tenant_id == claim.tenant_id, c.run_id == claim.run_id)
                    .distinct()
                )
            ).all()
        used: dict[str, list[str]] = {}
        for role, model in rows:
            used.setdefault(role, []).append(model)
        return {role: sorted(models) for role, models in sorted(used.items())}

    async def save_result(self, claim: Claim, result: dict[str, Any]) -> None:
        r = RUNS.c
        async with self.holding(claim) as connection:
            await connection.execute(
                update(RUNS)
                .where(r.tenant_id == claim.tenant_id, r.run_id == claim.run_id)
                .values(result_json=json.dumps(result, sort_keys=True))
            )

    async def finish(self, claim: Claim, status: str, reason: str = "") -> bool:
        """End the run, if the claim is still its own. False if another replica has it."""
        if status not in TERMINAL:
            raise ValueError(f"Not a terminal state: {status}")
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            row = (
                await connection.execute(select(RUNS).where(*self._mine(claim)).with_for_update())
            ).one_or_none()
            if row is None:
                return False
            await _end(connection, row, status=status, reason=reason, now=self._clock())
            return True

    async def retry_later(self, claim: Claim, reason: str, delay_seconds: float) -> bool:
        """A failed attempt: queue the run again after a pause, or fail it if none are left."""
        if claim.attempt >= claim.max_attempts:
            return await self.finish(claim, FAILED, reason)
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            now = self._clock()
            result = await connection.execute(
                update(RUNS)
                .where(*self._mine(claim))
                .values(
                    status=QUEUED, reason=reason, lease_token="", lease_owner="",
                    lease_expires_at=None, not_before=now + timedelta(seconds=delay_seconds),
                    updated_at=now,
                )
            )  # fmt: skip
            return result.rowcount == 1

    async def pause(self, claim: Claim) -> bool:
        """Stop the run as paused, as asked, keeping its checkpoints. Not an attempt."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            now = self._clock()
            r = RUNS.c
            result = await connection.execute(
                update(RUNS)
                .where(*self._mine(claim))
                .values(
                    status=PAUSED, attempt=case((r.attempt > 0, r.attempt - 1), else_=0),
                    lease_token="", lease_owner="", lease_expires_at=None, updated_at=now,
                )
            )  # fmt: skip
            return result.rowcount == 1

    async def record_sandbox(self, claim: Claim, use: "SandboxUse") -> None:
        """Audit an agent's Sandbox task, as the agent, on the user's behalf, under the run's
        lease (SBX-003, ADR 0024). Its code, hashes and runtime are in the run's trace."""
        by = claim.requested_by
        user = by.removeprefix("user:") if by.startswith("user:") else ""
        async with self.holding(claim) as connection:
            await audit(
                connection, tenant_id=claim.tenant_id, actor_type="agent",
                actor_id=f"agent:{use.agent}", on_behalf_of=user,
                action=f"agent_run.sandbox.{use.task}", run_id=claim.run_id,
                outcome="succeeded" if use.status == "completed" else "failed",
                resource=("sandbox_task", f"{use.task}@{use.version}"),
            )  # fmt: skip

    async def release(self, claim: Claim) -> bool:
        """Hand the run back for another replica now, as when shutting down. Not an attempt."""
        async with self._engine.begin() as connection:
            await in_tenant(connection, claim.tenant_id)
            now = self._clock()
            r = RUNS.c
            result = await connection.execute(
                update(RUNS)
                .where(*self._mine(claim))
                .values(
                    status=QUEUED, attempt=case((r.attempt > 0, r.attempt - 1), else_=0),
                    lease_token="", lease_owner="", lease_expires_at=None, not_before=now,
                    updated_at=now,
                )
            )  # fmt: skip
            return result.rowcount == 1

    @staticmethod
    def _mine(claim: Claim) -> tuple[Any, ...]:
        r = RUNS.c
        return (
            r.tenant_id == claim.tenant_id,
            r.run_id == claim.run_id,
            r.status == RUNNING,
            r.lease_token == claim.token,
        )


async def _spent(connection: AsyncConnection, claim: Claim) -> Spent:
    c = RUN_CALLS.c
    tokens, cost = (
        await connection.execute(
            select(func.coalesce(func.sum(c.processed_tokens), 0),
                   func.coalesce(func.sum(c.cost_usd), 0))
            .where(c.tenant_id == claim.tenant_id, c.run_id == claim.run_id)
        )
    ).one()  # fmt: skip
    return Spent(tokens=int(tokens), cost_usd=Decimal(str(cost)))


async def _end(
    connection: AsyncConnection, row: Any, *, status: str, reason: str, now: datetime
) -> None:
    r = RUNS.c
    await connection.execute(
        update(RUNS)
        .where(r.tenant_id == row.tenant_id, r.run_id == row.run_id)
        .values(
            status=status, reason=reason, finished_at=now, lease_token="", lease_owner="",
            lease_expires_at=None, updated_at=now,
        )
    )  # fmt: skip
    await audit(
        connection, tenant_id=row.tenant_id, actor_type="service", actor_id=ACTOR,
        action=f"agent_run.{status}", run_id=row.run_id,
        outcome="succeeded" if status != FAILED else "failed",
    )  # fmt: skip
