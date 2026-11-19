"""Each case's investigation: started in the Agent Runtime, followed, and kept as progress (M8.11).

    requested ──evidence frozen──> started (case: investigating) ──run completes──> pending_review
        │                                  └──run ends otherwise──> open again
        └──case cancelled, or its delegation expired first──> not started

**Who asks:** a case from detections is investigated as the tenant's `fault-investigator`; a
case a person asks to investigate, under that person's delegation, started with their own
Keycloak token while they wait for the case's ID (ADR 0019). Either way the run is started by
this service, acting as itself, as the only caller the Agent Runtime lets in.

**When:** a run needs a frozen snapshot (EVD-001), so the keeper starts it once the case's first
is frozen, and the case moves to `investigating`.

**Following it:** the keeper reads each running investigation's run, adds every newly completed
step to the case's progress, and, when the run ends, moves the case: `pending_review` when it
completed (SRS 5.4 step 13), or back to `open`, with why, when it ended any other way. A case
cancelled while its run is going cancels the run.

**Progress:** each event has a sequence from 1 in its case, which the live stream sends as its
event ID (API-003). An event is keyed by what it records, so two replicas following the same
run add each step once. Events never change.

**What a run costs:** `WINDOPS_INVESTIGATION_AGENTS` says how runs are started. `rehearse` runs
the real team on a scripted model, with no call to Claude; `claude` runs it on Claude; `off`
starts none. The deployments say `rehearse` until the exit gate (M8.13) turns Claude on.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import grpc
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.agent_runtime.v1 import agent_runtime_pb2
from windops.identity.v1 import identity_pb2
from windops_grpc import (
    DomainError,
    PermissionDeniedError,
    ServiceClient,
    UnavailableError,
    acting_as_service,
    service_subject,
)
from windops_migrations import across_tenants, in_tenant
from windops_resilience import BreakerOpenError

from windops_investigation import cases
from windops_investigation.store import (
    CANCELLED,
    CASES,
    INVESTIGATING,
    INVESTIGATIONS,
    OPEN,
    PENDING_REVIEW,
    PROGRESS,
    SERVICE_NAME,
)

logger = logging.getLogger("windops.service")
KEEPER = service_subject(SERVICE_NAME)
REQUESTED_BY_SERVICE = f"service:{SERVICE_NAME}"
WAITING, RUNNING, COMPLETED, ENDED, NOT_STARTED = (
    "waiting", "running", "completed", "ended", "not_started",
)  # fmt: skip
OVER = (COMPLETED, ENDED, NOT_STARTED)
MODES = ("rehearse", "claude", "off")
# The longest a delegation may last (ADR 0019), so a run has the time it needs.
DELEGATION_SECONDS = 4 * 3600
# A run needs time left under its delegation; one with less is not started.
LEAST_LEFT = timedelta(minutes=30)
RUN_TERMINAL = ("completed", "failed", "cancelled", "budget_exhausted", "stale")


class Runtime(Protocol):
    """The Agent Runtime, asked as this service in the case's tenant."""

    async def start_run(
        self, *, case_id: str, snapshot_id: str, parameters: dict[str, str], requested_by: str,
        delegation_id: str, key: str,
    ) -> Any: ...  # fmt: skip

    async def get_run(self, run_id: str) -> Any: ...

    async def cancel_run(self, run_id: str, reason: str) -> Any: ...


class Delegations(Protocol):
    """The Identity Service: (delegation ID, when it expires)."""

    async def for_user(self, case_id: str, subject_token: str) -> tuple[str, datetime]: ...

    async def for_fault_investigator(self, case_id: str) -> tuple[str, datetime]: ...


def parameters_for(mode: str, turbine_id: str) -> dict[str, str]:
    """The investigation workflow's parameters: the team, rehearsed unless Claude is on."""
    if mode == "claude":
        return {"team": "true"}
    return {"team": "true", "rehearse": "true", "turbine_id": turbine_id}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def add_progress(
    connection: AsyncConnection, case: Any, *, key: str, kind: str, at: datetime, **values: Any
) -> bool:
    """Add one event, after the case's last, unless one with this key is there. The caller holds
    the case's row lock, so two replicas never take the same sequence."""
    p = PROGRESS.c
    seen = await connection.execute(
        select(p.sequence).where(p.tenant_id == case.tenant_id, p.case_id == case.case_id,
                                 p.event_key == key)
    )  # fmt: skip
    if seen.first() is not None:
        return False
    last = (await connection.execute(
        select(func.max(p.sequence)).where(p.tenant_id == case.tenant_id, p.case_id == case.case_id)
    )).scalar()  # fmt: skip
    await connection.execute(PROGRESS.insert().values(
        tenant_id=case.tenant_id, case_id=case.case_id, sequence=(last or 0) + 1, at=at,
        kind=kind, event_key=key, **values,
    ))  # fmt: skip
    return True


async def request(
    connection: AsyncConnection, case: Any, *, requested_by: str, now: datetime,
    delegation: tuple[str, datetime] | None = None,
) -> None:  # fmt: skip
    """Want an investigation for a new case, started once its evidence is frozen."""
    delegation_id, expires = delegation or ("", None)
    await connection.execute(INVESTIGATIONS.insert().values(
        tenant_id=case.tenant_id, case_id=case.case_id, status=WAITING,
        requested_by=requested_by, delegation_id=delegation_id, delegation_expires_at=expires,
        requested_at=now, updated_at=now,
    ))  # fmt: skip
    await add_progress(connection, case, key="requested", kind="requested", at=now,
                       note="Waiting for the case's evidence to be frozen")  # fmt: skip


async def load(connection: AsyncConnection, tenant: str, case_id: str) -> Any | None:
    i = INVESTIGATIONS.c
    return (await connection.execute(
        select(INVESTIGATIONS).where(i.tenant_id == tenant, i.case_id == case_id)
    )).one_or_none()  # fmt: skip


async def _locked_case(connection: AsyncConnection, tenant: str, case_id: str) -> Any:
    await in_tenant(connection, tenant)
    return (await connection.execute(
        select(CASES).where(CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
        .with_for_update()
    )).one()  # fmt: skip


async def _set(connection: AsyncConnection, case: Any, now: datetime, **values: Any) -> None:
    i = INVESTIGATIONS.c
    await connection.execute(
        update(INVESTIGATIONS).where(i.tenant_id == case.tenant_id, i.case_id == case.case_id)
        .values(updated_at=now, **values)
    )  # fmt: skip


@dataclass
class Keeper:
    """Starts and follows investigations, in every tenant, as this service."""

    engine: AsyncEngine
    runtime: Runtime
    delegations: Delegations
    secret: str
    mode: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def run_once(self) -> int:
        """Start what is ready, and follow what is running. Returns how many changed."""
        async with self.engine.connect() as connection:
            await across_tenants(connection)
            i, c = INVESTIGATIONS.c, CASES.c
            ready = (await connection.execute(
                select(i.tenant_id, i.case_id)
                .join(CASES, (c.tenant_id == i.tenant_id) & (c.case_id == i.case_id))
                .where(i.status == WAITING, (c.evidence_snapshot_id != "") | (c.status != OPEN))
                .order_by(i.requested_at)
            )).all()  # fmt: skip
            running = (await connection.execute(
                select(i.tenant_id, i.case_id).where(i.status == RUNNING).order_by(i.started_at)
            )).all()  # fmt: skip
        changed = 0
        for tenant, case_id in ready:
            changed += await self._guarded(self.start, tenant, case_id)
        for tenant, case_id in running:
            changed += await self._guarded(self.follow, tenant, case_id)
        return changed

    async def _guarded(self, step: Any, tenant: str, case_id: str) -> int:
        try:
            with acting_as_service(SERVICE_NAME, tenant, self.secret):
                return 1 if await step(tenant, case_id) else 0
        except DomainError as error:
            # The runtime or the Identity Service cannot be reached: tried again next time.
            logger.warning(
                "Could not move an investigation on",
                extra={"case_id": case_id, "error_type": type(error).__name__},
            )
            return 0

    async def start(self, tenant: str, case_id: str) -> bool:
        now = self.clock()
        async with self.engine.connect() as connection:
            await in_tenant(connection, tenant)
            case = await cases.load(connection, tenant, case_id)
            wanted = await load(connection, tenant, case_id)
        if case is None or wanted is None or wanted.status != WAITING:
            return False
        if self.mode == "off" and case.status == OPEN:
            return False
        if case.status != OPEN:
            return await self._not_started(tenant, case_id, f"The case is {case.status}")
        delegation, expires = wanted.delegation_id, wanted.delegation_expires_at
        if not delegation:
            delegation, expires = await self.delegations.for_fault_investigator(case_id)
            async with self.engine.begin() as connection:
                locked = await _locked_case(connection, tenant, case_id)
                await _set(connection, locked, now, delegation_id=delegation,
                           delegation_expires_at=expires)  # fmt: skip
        if expires is None or _utc(expires) - now < LEAST_LEFT:
            return await self._not_started(
                tenant, case_id, "Its delegation ran out before its evidence was frozen"
            )
        run = await self.runtime.start_run(
            case_id=case_id, snapshot_id=case.evidence_snapshot_id,
            parameters=parameters_for(self.mode, case.turbine_id),
            requested_by=wanted.requested_by, delegation_id=delegation,
            key=f"investigate-{case_id}",
        )  # fmt: skip
        async with self.engine.begin() as connection:
            locked = await _locked_case(connection, tenant, case_id)
            await _set(connection, locked, now, status=RUNNING, run_id=run.run_id,
                       snapshot_id=run.snapshot_id, started_at=now)  # fmt: skip
            await add_progress(connection, locked, key=f"started:{run.run_id}", kind="started",
                               at=now, run_id=run.run_id, total_steps=run.total_steps)  # fmt: skip
            if locked.status == OPEN:
                moved = await cases.change(
                    connection, locked, now=now, action="investigation_started",
                    actor_type=cases.SERVICE, actor_id=KEEPER, note=run.run_id,
                    status=INVESTIGATING,
                )  # fmt: skip
                await add_progress(connection, moved, key=f"case:{moved.version}",
                                   kind="case_status", at=now, status=INVESTIGATING)  # fmt: skip
        return True

    async def _not_started(self, tenant: str, case_id: str, why: str) -> bool:
        now = self.clock()
        async with self.engine.begin() as connection:
            locked = await _locked_case(connection, tenant, case_id)
            await _set(connection, locked, now, status=NOT_STARTED, note=why, ended_at=now)
            await add_progress(connection, locked, key="not_started", kind="run_ended", at=now,
                               status=NOT_STARTED, note=why)  # fmt: skip
            if locked.status == OPEN:
                await cases.change(connection, locked, now=now,
                                   action="investigation_not_started",
                                   actor_type=cases.SERVICE, actor_id=KEEPER, note=why)  # fmt: skip
        return True

    async def follow(self, tenant: str, case_id: str) -> bool:
        async with self.engine.connect() as connection:
            await in_tenant(connection, tenant)
            wanted = await load(connection, tenant, case_id)
            case = await cases.load(connection, tenant, case_id)
        if wanted is None or wanted.status != RUNNING:
            return False
        if case is None:
            # The case is gone, as a check's clean-up removes its own: stop following.
            async with self.engine.begin() as connection:
                await in_tenant(connection, tenant)
                i = INVESTIGATIONS.c
                await connection.execute(
                    update(INVESTIGATIONS).where(i.tenant_id == tenant, i.case_id == case_id)
                    .values(status=ENDED, note="The case is gone", ended_at=self.clock(),
                            updated_at=self.clock())
                )  # fmt: skip
            return True
        run = await self.runtime.get_run(wanted.run_id)
        if case.status == CANCELLED and run.status not in RUN_TERMINAL and not run.cancel_requested:
            run = await self.runtime.cancel_run(run.run_id, "The case was cancelled")
        now = self.clock()
        added = False
        async with self.engine.begin() as connection:
            locked = await _locked_case(connection, tenant, case_id)
            for step in run.steps:
                added |= await add_progress(
                    connection, locked, key=f"step:{run.run_id}:{step.position}", kind="step",
                    at=step.finished_at.ToDatetime(tzinfo=UTC) if step.HasField("finished_at")
                    else now,
                    run_id=run.run_id, position=step.position, total_steps=run.total_steps,
                    step=step.name,
                )  # fmt: skip
            if run.status not in RUN_TERMINAL:
                return added
            done = run.status == "completed"
            await add_progress(connection, locked, key=f"ended:{run.run_id}", kind="run_ended",
                               at=now, run_id=run.run_id, status=run.status,
                               note=run.reason[:500])  # fmt: skip
            await _set(connection, locked, now, status=COMPLETED if done else ENDED,
                       note=run.reason[:500], ended_at=now)  # fmt: skip
            if locked.status == INVESTIGATING:
                # SRS 5.4 step 13: a completed investigation waits for a person's review.
                moved = await cases.change(
                    connection, locked, now=now,
                    action="investigation_completed" if done else "investigation_ended",
                    actor_type=cases.SERVICE, actor_id=KEEPER,
                    note=run.run_id if done else f"{run.status}: {run.reason}"[:500],
                    status=PENDING_REVIEW if done else OPEN,
                )  # fmt: skip
                await add_progress(connection, moved, key=f"case:{moved.version}",
                                   kind="case_status", at=now, status=moved.status)  # fmt: skip
        return True


async def keep_investigating(
    keeper: Keeper, stop: asyncio.Event, every_seconds: float = 2.0
) -> None:
    while not stop.is_set():
        try:
            await keeper.run_once()
        except Exception as error:  # Tried again shortly. The cases are safe meanwhile.
            logger.warning("Could not run the investigation keeper",
                           extra={"error_type": type(error).__name__})  # fmt: skip
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)


DEADLINE_SECONDS = 5.0


async def _ask(client: ServiceClient, method: Any, request: Any, who: str) -> Any:
    try:
        return await client.call(method, request, idempotent=True,
                                 deadline_seconds=DEADLINE_SECONDS)  # fmt: skip
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.UNAUTHENTICATED):
            raise PermissionDeniedError(f"{who} refused to act for this caller") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class RuntimeClient:
    """The Agent Runtime, as this service: the only caller it lets in (ADR 0016)."""

    def __init__(self, stub: Callable[[], Any], client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def start_run(
        self, *, case_id: str, snapshot_id: str, parameters: dict[str, str], requested_by: str,
        delegation_id: str, key: str,
    ) -> Any:  # fmt: skip
        request = agent_runtime_pb2.StartRunRequest(
            case_id=case_id, snapshot_id=snapshot_id, workflow="investigation",
            parameters=parameters, requested_by=requested_by, idempotency_key=key,
            delegation_id=delegation_id,
        )  # fmt: skip
        return (await _ask(self._client, self._stub().StartRun, request, "The Agent Runtime")).run

    async def get_run(self, run_id: str) -> Any:
        request = agent_runtime_pb2.GetRunRequest(run_id=run_id)
        return (await _ask(self._client, self._stub().GetRun, request, "The Agent Runtime")).run

    async def cancel_run(self, run_id: str, reason: str) -> Any:
        request = agent_runtime_pb2.CancelRunRequest(
            run_id=run_id, requested_by=REQUESTED_BY_SERVICE, reason=reason,
            idempotency_key=f"cancel-{run_id}",
        )  # fmt: skip
        return (await _ask(self._client, self._stub().CancelRun, request, "The Agent Runtime")).run


class DelegationClient:
    """The Identity Service. A person's delegation is asked as that person, with their token; the
    fault-investigator's as this service (ADR 0019). The token is passed on, never kept."""

    def __init__(self, stub: Callable[[], Any], client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def _start(self, case_id: str, subject_token: str) -> tuple[str, datetime]:
        request = identity_pb2.StartDelegationRequest(
            case_id=case_id, subject_token=subject_token, ttl_seconds=DELEGATION_SECONDS,
            idempotency_key=f"investigate-{case_id}",
        )  # fmt: skip
        answer = await _ask(self._client, self._stub().StartDelegation, request,
                            "The Identity Service")  # fmt: skip
        delegation = answer.delegation
        return delegation.delegation_id, delegation.expires_at.ToDatetime(tzinfo=UTC)

    async def for_user(self, case_id: str, subject_token: str) -> tuple[str, datetime]:
        return await self._start(case_id, subject_token)

    async def for_fault_investigator(self, case_id: str) -> tuple[str, datetime]:
        return await self._start(case_id, "")
