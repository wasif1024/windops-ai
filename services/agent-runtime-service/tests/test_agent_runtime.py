"""The queue, its workers and the API, on SQLite. SQLite has no row-level security, row locks or
triggers, so runtime-check proves those, and two real replicas, on PostgreSQL."""

import asyncio
import contextlib
import functools
import inspect
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.agent_runtime.v1 import agent_runtime_pb2 as pb
from windops_agent_runtime.queue import LeaseLostError, Queue
from windops_agent_runtime.service import INVESTIGATION, AgentRuntimeService
from windops_agent_runtime.store import METADATA, RUN_STEPS, RUNS
from windops_agent_runtime.worker import Worker, WorkerSettings
from windops_agent_runtime.workflows import REGISTRY, Rehearsal
from windops_auth import identity_scope
from windops_events import parse_event
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    VerifiedIdentity,
)
from windops_outbox import OUTBOX_TABLE, create_outbox_table

TENANT, OTHER = "north-sea", "atlas-wind"
CASE = "case_" + "a" * 20
SNAPSHOT = "evs_" + "b" * 20
START = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
LEASE = 30.0


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def scenario(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """Run an async test on a fresh database, passing it what it names: engine, clock, queue."""

    @functools.wraps(test)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        async def body() -> None:
            engine = create_async_engine(
                "sqlite+aiosqlite://", poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )  # fmt: skip
            async with engine.begin() as connection:
                await connection.run_sync(METADATA.create_all)
                await connection.run_sync(create_outbox_table)
            clock = Clock()
            queue = Queue(engine, lease_seconds=LEASE, queue_limit=timedelta(hours=24), clock=clock)
            given = {"engine": engine, "clock": clock, "queue": queue}
            wanted = inspect.signature(test).parameters
            try:
                await test(*args, **kwargs, **{k: v for k, v in given.items() if k in wanted})
            finally:
                await engine.dispose()

        asyncio.run(body())

    # pytest must see only the parameters it supplies itself.
    parameters = [
        p for name, p in inspect.signature(test).parameters.items()
        if name not in {"engine", "clock", "queue"}
    ]  # fmt: skip
    wrapper.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    return wrapper


def service(
    engine: AsyncEngine, clock: Clock, peer: str | None = INVESTIGATION
) -> AgentRuntimeService:
    return AgentRuntimeService(engine, REGISTRY, max_attempts=3, peer=lambda: peer, clock=clock)


@contextlib.contextmanager
def caller(tenant: str = TENANT) -> Iterator[None]:
    identity = VerifiedIdentity(subject="service:investigation", tenant_id=tenant)
    with identity_scope(identity, "token"):
        yield


def start_request(key: str = "start-key-1", **parameters: str) -> pb.StartRunRequest:
    return pb.StartRunRequest(
        case_id=CASE, snapshot_id=SNAPSHOT, workflow="rehearsal",
        parameters={"steps": "3", "step_seconds": "0", **parameters},
        requested_by="user:engineer-1", idempotency_key=key,
    )  # fmt: skip


async def start(
    engine: AsyncEngine, clock: Clock, key: str = "start-key-1", **parameters: str
) -> pb.Run:
    with caller():
        return (await service(engine, clock).StartRun(start_request(key, **parameters), None)).run


async def audits(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE))).all()
    return [parse_event(row.subject, row.envelope).payload.action for row in rows]  # type: ignore[attr-defined]


async def status(engine: AsyncEngine, run_id: str) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(RUNS).where(RUNS.c.run_id == run_id))).one()


async def steps(engine: AsyncEngine, run_id: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(RUN_STEPS)
                    .where(RUN_STEPS.c.run_id == run_id)
                    .order_by(RUN_STEPS.c.position)
                )
            ).all()
        )


# --- The API


@scenario
async def test_a_run_is_queued_at_once_pinned_and_audited(
    engine: AsyncEngine, clock: Clock
) -> None:
    run = await start(engine, clock)
    assert run.status == "queued"
    assert run.run_id.startswith("run_") and len(run.run_id) == 24
    assert (run.workflow, run.workflow_version) == ("rehearsal", "1")
    assert (run.attempt, run.max_attempts, run.total_steps) == (0, 3, 3)
    assert run.requested_by == "user:engineer-1"
    assert await audits(engine) == ["agent_run.queued"]


@scenario
async def test_the_same_key_returns_the_same_run_and_a_different_request_is_refused(
    engine: AsyncEngine, clock: Clock
) -> None:
    first = await start(engine, clock)
    again = await start(engine, clock)
    assert again.run_id == first.run_id
    with pytest.raises(ConflictError):
        await start(engine, clock, steps="4")
    assert await audits(engine) == ["agent_run.queued"]


@scenario
async def test_only_the_investigation_service_may_call(engine: AsyncEngine, clock: Clock) -> None:
    run = await start(engine, clock)
    for peer in (None, "windops-web-bff", "windops-check-client"):
        refused = service(engine, clock, peer)
        with caller(), pytest.raises(PermissionDeniedError):
            await refused.StartRun(start_request("start-key-2"), None)
        with caller(), pytest.raises(PermissionDeniedError):
            await refused.GetRun(pb.GetRunRequest(run_id=run.run_id), None)


@scenario
async def test_another_tenant_never_sees_a_run(engine: AsyncEngine, clock: Clock) -> None:
    run = await start(engine, clock)
    api = service(engine, clock)
    with caller(OTHER):
        with pytest.raises(NotFoundError):
            await api.GetRun(pb.GetRunRequest(run_id=run.run_id), None)
        listed = await api.ListRuns(pb.ListRunsRequest(case_id=CASE), None)
        assert not listed.runs
        with pytest.raises(NotFoundError):
            await api.CancelRun(
                pb.CancelRunRequest(run_id=run.run_id, requested_by="user:x", reason="no"), None
            )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"workflow": "investigation"}, "No workflow"),
        ({"case_id": "case_1"}, "case_id"),
        ({"snapshot_id": "snap"}, "snapshot_id"),
        ({"requested_by": "engineer"}, "requested_by"),
        ({"idempotency_key": "short"}, "idempotency_key"),
    ],
)
@scenario
async def test_a_bad_request_is_refused(
    engine: AsyncEngine, clock: Clock, change: dict[str, str], message: str
) -> None:
    request = start_request()
    for field, value in change.items():
        setattr(request, field, value)
    with caller(), pytest.raises(InvalidRequestError, match=message):
        await service(engine, clock).StartRun(request, None)


@pytest.mark.parametrize(
    "parameters",
    [{"steps": "0"}, {"steps": "many"}, {"step_seconds": "-1"}, {"fail_at_step": "9"}, {"x": "1"}],
)
def test_rehearsal_parameters_are_checked(parameters: dict[str, str]) -> None:
    with pytest.raises(InvalidRequestError):
        Rehearsal().check({"steps": "3", **parameters} if "steps" not in parameters else parameters)


@scenario
async def test_runs_of_a_case_are_listed_newest_first(engine: AsyncEngine, clock: Clock) -> None:
    first = await start(engine, clock, "start-key-1")
    clock.advance(1)
    second = await start(engine, clock, "start-key-2")
    with caller():
        listed = await service(engine, clock).ListRuns(pb.ListRunsRequest(case_id=CASE), None)
    assert [run.run_id for run in listed.runs] == [second.run_id, first.run_id]


@scenario
async def test_a_queued_run_is_cancelled_at_once(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock)
    request = pb.CancelRunRequest(
        run_id=run.run_id, requested_by="user:engineer-1", reason="Wrong turbine"
    )
    with caller():
        cancelled = (await service(engine, clock).CancelRun(request, None)).run
        again = (await service(engine, clock).CancelRun(request, None)).run
    assert (cancelled.status, cancelled.cancel_requested) == ("cancelled", True)
    assert again.status == "cancelled"
    assert await queue.claim("worker/0") is None
    assert await audits(engine) == [
        "agent_run.queued", "agent_run.cancel_requested", "agent_run.cancelled",
    ]  # fmt: skip


# --- The queue and its workers


@scenario
async def test_a_worker_runs_each_step_and_completes(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None and claim.attempt == 1 and not claim.reclaimed
    outcome = await Worker(queue, REGISTRY).execute(claim, asyncio.Event())
    assert outcome == "completed"
    row = await status(engine, run.run_id)
    assert (row.status, row.attempt, row.lease_token) == ("completed", 1, "")
    assert [(s.position, s.name, s.worker) for s in await steps(engine, run.run_id)] == [
        (1, "step-1", "replica-a/0"), (2, "step-2", "replica-a/0"), (3, "step-3", "replica-a/0"),
    ]  # fmt: skip
    assert (await audits(engine))[-1] == "agent_run.completed"


@scenario
async def test_two_workers_never_hold_the_same_run(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    await start(engine, clock)
    assert await queue.claim("replica-a/0") is not None
    assert await queue.claim("replica-b/0") is None


@scenario
async def test_a_crashed_workers_run_is_taken_over_after_its_last_completed_step(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock, steps="4")
    crashed = await queue.claim("replica-a/0")
    assert crashed is not None
    for position in (1, 2):
        await queue.complete_step(
            crashed, position=position, name=f"step-{position}", started_at=clock()
        )
    # It stops renewing. Before the lease runs out, nobody else may take the run.
    clock.advance(LEASE - 1)
    assert await queue.claim("replica-b/0") is None
    clock.advance(2)
    taken = await queue.claim("replica-b/0")
    assert taken is not None
    assert (taken.attempt, taken.reclaimed, taken.completed) == (2, True, frozenset({1, 2}))
    # The crashed worker, back too late, can change nothing.
    with pytest.raises(LeaseLostError):
        await queue.complete_step(crashed, position=3, name="step-3", started_at=clock())
    assert not (await queue.renew(crashed)).held
    assert not await queue.finish(crashed, "completed")
    assert await Worker(queue, REGISTRY).execute(taken, asyncio.Event()) == "completed"
    done = await steps(engine, run.run_id)
    assert [(s.position, s.attempt, s.worker) for s in done] == [
        (1, 1, "replica-a/0"), (2, 1, "replica-a/0"), (3, 2, "replica-b/0"), (4, 2, "replica-b/0"),
    ]  # fmt: skip
    assert (await status(engine, run.run_id)).status == "completed"


@scenario
async def test_a_worker_shutting_down_hands_its_run_back_without_spending_an_attempt(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock, steps="3", step_seconds="5")
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    stop = asyncio.Event()
    working = asyncio.create_task(Worker(queue, REGISTRY).execute(claim, stop))
    await asyncio.sleep(0.05)
    stop.set()
    assert await working == "released"
    row = await status(engine, run.run_id)
    assert (row.status, row.attempt, row.lease_token) == ("queued", 0, "")
    again = await queue.claim("replica-b/0")
    assert again is not None and again.attempt == 1 and not again.reclaimed


@scenario
async def test_a_failing_step_is_tried_again_then_the_run_fails(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock, steps="3", fail_at_step="2")
    worker = Worker(queue, REGISTRY, WorkerSettings(retry_seconds=5))
    outcomes = []
    for _ in range(3):
        claim = await queue.claim("replica-a/0")
        assert claim is not None
        outcomes.append(await worker.execute(claim, asyncio.Event()))
        # Not claimed again until its pause is over.
        assert await queue.claim("replica-a/0") is None or claim.attempt == 3
        clock.advance(60)
    assert outcomes == ["retried", "retried", "failed"]
    row = await status(engine, run.run_id)
    assert (row.status, row.attempt) == ("failed", 3)
    assert row.reason == "The rehearsal was asked to fail here"
    # Step 1 was done once, by the first attempt, and never again.
    assert [(s.position, s.attempt) for s in await steps(engine, run.run_id)] == [(1, 1)]
    assert (await audits(engine))[-1] == "agent_run.failed"


@scenario
async def test_a_run_whose_last_attempt_crashed_fails_instead_of_being_claimed(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock)
    for _ in range(3):
        assert await queue.claim("replica-a/0") is not None
        clock.advance(LEASE + 1)
    assert await queue.claim("replica-b/0") is None
    row = await status(engine, run.run_id)
    assert (row.status, row.reason) == ("failed", "Stopped after 3 attempts")


@scenario
async def test_a_running_run_is_cancelled_at_its_next_heartbeat(
    engine: AsyncEngine, queue: Queue
) -> None:
    # A real clock: the heartbeat sleeps for a third of the lease.
    live = Queue(engine, lease_seconds=0.3, queue_limit=timedelta(hours=24))
    with caller():
        run = (await AgentRuntimeService(engine, REGISTRY, peer=lambda: INVESTIGATION).StartRun(
            start_request(steps="5", step_seconds="1"), None)).run  # fmt: skip
    claim = await live.claim("replica-a/0")
    assert claim is not None
    working = asyncio.create_task(Worker(live, REGISTRY).execute(claim, asyncio.Event()))
    await asyncio.sleep(0.05)
    with caller():
        asked = (await AgentRuntimeService(engine, REGISTRY, peer=lambda: INVESTIGATION).CancelRun(
            pb.CancelRunRequest(run_id=run.run_id, requested_by="user:engineer-1",
                                reason="Not needed"), None)).run  # fmt: skip
    assert (asked.status, asked.cancel_requested) == ("running", True)
    assert await asyncio.wait_for(working, timeout=5) == "cancelled"
    row = await status(engine, run.run_id)
    assert (row.status, row.reason) == ("cancelled", "Cancelled")


@scenario
async def test_a_run_that_waited_too_long_is_stale(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock)
    clock.advance(25 * 3600)
    assert await queue.claim("replica-a/0") is None
    assert (await status(engine, run.run_id)).status == "stale"


@scenario
async def test_a_run_never_continues_on_another_workflow_version(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    changed = {"rehearsal": Rehearsal(version="2")}
    assert await Worker(queue, changed).execute(claim, asyncio.Event()) == "stale"
    row = await status(engine, run.run_id)
    assert row.status == "stale" and "version" in row.reason


@scenario
async def test_a_slot_claims_and_runs_runs_until_stopped(engine: AsyncEngine, clock: Clock) -> None:
    live = Queue(engine, lease_seconds=30, queue_limit=timedelta(hours=24))
    with caller():
        api = AgentRuntimeService(engine, REGISTRY, peer=lambda: INVESTIGATION)
        first = (await api.StartRun(start_request("start-key-1"), None)).run
        second = (await api.StartRun(start_request("start-key-2"), None)).run
    stop = asyncio.Event()
    worker = Worker(live, REGISTRY, WorkerSettings(slots=2, poll_seconds=0.02), name="replica-a")
    running = asyncio.create_task(worker.run_forever(stop))
    for _ in range(200):
        rows = [await status(engine, run.run_id) for run in (first, second)]
        if all(row.status == "completed" for row in rows):
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(running, timeout=5)
    assert [row.status for row in rows] == ["completed", "completed"]


@scenario
async def test_a_worker_whose_run_was_taken_logs_and_changes_nothing(
    caplog: pytest.LogCaptureFixture, engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start(engine, clock, steps="2")
    late = await queue.claim("replica-a/0")
    assert late is not None
    clock.advance(LEASE + 1)
    taken = await queue.claim("replica-b/0")
    assert taken is not None
    with caplog.at_level("WARNING", logger="windops.agent_runtime"):
        assert await Worker(queue, REGISTRY).execute(late, asyncio.Event()) == "lost"
    assert "Lost a run's lease" in caplog.text
    assert not await steps(engine, run.run_id)
    assert (await status(engine, run.run_id)).lease_owner == "replica-b/0"
