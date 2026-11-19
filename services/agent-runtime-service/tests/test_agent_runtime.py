"""The queue, its workers and the API, on SQLite. SQLite has no row-level security, row locks or
triggers, so runtime-check proves those, and two real replicas, on PostgreSQL."""

import asyncio
import contextlib
import dataclasses
import functools
import inspect
import json
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from windops.agent_runtime.v1 import agent_runtime_pb2 as pb
from windops_agent_runtime.checkpoints import RunCheckpointer
from windops_agent_runtime.investigation import STEPS, Investigation, build
from windops_agent_runtime.queue import LeaseLostError, Queue
from windops_agent_runtime.registry import REGISTRY
from windops_agent_runtime.service import INVESTIGATION, AgentRuntimeService
from windops_agent_runtime.store import METADATA, RUN_CALLS, RUN_CHECKPOINTS, RUN_STEPS, RUNS
from windops_agent_runtime.worker import Worker, WorkerSettings
from windops_agent_runtime.workflows import Rehearsal
from windops_auth import identity_scope
from windops_claude import Call, Claude, Policy, Usage, load
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
POLICY = load()


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
            # A file, with a connection per use, as workers, heartbeats and the test's own reads
            # overlap. One shared connection would let one's rollback undo another's writes.
            folder = tempfile.mkdtemp(prefix="windops-runtime-test-")
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{folder}/runs.db", poolclass=NullPool,
                connect_args={"timeout": 30},
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
                shutil.rmtree(folder, ignore_errors=True)

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
        ({"workflow": "unknown-workflow"}, "No workflow"),
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


# --- The Claude policy in a run (M8.3)


class FakeChat:
    """Stands in for ChatAnthropic: answers at once, with the usage Claude would report."""

    def __init__(self, stop: str, max_tokens: int) -> None:
        self.stop, self.max_tokens = stop, max_tokens

    def _answer(self, text: str) -> AIMessage:
        return AIMessage(
            content=text,
            response_metadata={
                "model": "claude-opus-5-5", "stop_reason": self.stop,
                "usage": {"input_tokens": 400, "output_tokens": 100,
                          "cache_read_input_tokens": 1000},
            },
        )  # fmt: skip

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        return self._answer("ready")

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        chat = self

        class Bound:
            async def ainvoke(self, messages: list[Any]) -> dict[str, Any]:
                parsed = schema(ready=True, role="supervisor")
                return {"raw": chat._answer(parsed.model_dump_json()), "parsed": parsed,
                        "parsing_error": None}  # fmt: skip

        return Bound()


def claude_worker(queue: Queue, stop: str = "end_turn", policy: Policy = POLICY) -> Worker:
    def make(claim: Any, meter: Any) -> Claude:
        def model(_: Any, role: str, *, api_key: str, max_tokens: int) -> FakeChat:
            return FakeChat(stop, max_tokens)

        return Claude(policy, meter, lambda: "key", make_model=model)  # type: ignore[arg-type]

    return Worker(queue, REGISTRY, policy=policy, make_claude=make)


async def start_check(
    engine: AsyncEngine, clock: Clock, key: str = "check-key-1", **budget: Any
) -> pb.Run:
    request = pb.StartRunRequest(
        case_id=CASE, snapshot_id=SNAPSHOT, workflow="model-check",
        parameters={"roles": "supervisor,metadata-extraction"},
        requested_by="service:investigation", idempotency_key=key, **budget,
    )  # fmt: skip
    with caller():
        return (await service(engine, clock).StartRun(request, None)).run


async def get(engine: AsyncEngine, clock: Clock, run_id: str) -> pb.Run:
    with caller():
        return (await service(engine, clock).GetRun(pb.GetRunRequest(run_id=run_id), None)).run


@scenario
async def test_a_run_pins_the_policy_and_its_tenants_budget(
    engine: AsyncEngine, clock: Clock
) -> None:
    run = await start_check(engine, clock)
    assert (run.policy_version, run.policy_hash) == (POLICY.version, POLICY.content_hash)
    assert run.usage.token_budget == POLICY.budgets.default.tokens
    assert run.usage.cost_budget_usd == f"{POLICY.budgets.default.cost_usd:.6f}"
    lowered = await start_check(
        engine, clock, "check-key-2", token_budget=10**9, cost_budget_usd="0.5"
    )
    # A request may lower a budget, never raise it.
    assert lowered.usage.token_budget == POLICY.budgets.default.tokens
    assert lowered.usage.cost_budget_usd == "0.500000"
    for bad in ("-1", "lots", "NaN", "0"):
        with pytest.raises(InvalidRequestError):
            await start_check(engine, clock, f"check-key-{bad}", cost_budget_usd=bad)


@scenario
async def test_each_call_is_recorded_with_its_tokens_and_cost(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_check(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None and claim.token_budget == POLICY.budgets.default.tokens
    assert await claude_worker(queue).execute(claim, asyncio.Event()) == "completed"
    done = await get(engine, clock, run.run_id)
    # Two roles, a structured answer and the cached prompt: four calls of 500 processed tokens.
    assert (done.usage.calls, done.usage.tokens, done.usage.cache_read_tokens) == (4, 2000, 4000)
    # Each: 400 x $4 + 1000 x $0.20 + 100 x $20, per million.
    assert done.usage.cost_usd == "0.015200"
    async with engine.connect() as connection:
        calls = (await connection.execute(select(RUN_CALLS).order_by(RUN_CALLS.c.position))).all()
    assert [(c.role, c.effort, c.attempt) for c in calls] == [
        ("supervisor", "high", 1), ("metadata-extraction", "low", 1),
        ("supervisor", "high", 1), ("supervisor", "high", 1),
    ]  # fmt: skip


@scenario
async def test_a_run_that_reaches_its_budget_ends_as_budget_exhausted(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_check(engine, clock, token_budget=700)
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    assert await claude_worker(queue).execute(claim, asyncio.Event()) == "budget_exhausted"
    ended = await get(engine, clock, run.run_id)
    assert ended.status == "budget_exhausted" and "tokens" in ended.reason
    # The second call reached the budget, and no third was made.
    assert ended.usage.calls == 2
    assert (await audits(engine))[-1] == "agent_run.budget_exhausted"
    assert await queue.claim("replica-a/0") is None


@scenario
async def test_a_refusal_fails_the_run_without_trying_again(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_check(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    assert await claude_worker(queue, stop="refusal").execute(claim, asyncio.Event()) == "failed"
    ended = await get(engine, clock, run.run_id)
    assert (ended.status, ended.attempt, ended.usage.calls) == ("failed", 1, 1)
    assert ended.reason.startswith("RefusedError")


@scenario
async def test_a_run_never_continues_under_another_policy(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_check(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    changed = POLICY.model_copy(update={"version": POLICY.version + 1})
    assert await claude_worker(queue, policy=changed).execute(claim, asyncio.Event()) == "stale"
    assert "policy" in (await get(engine, clock, run.run_id)).reason


@scenario
async def test_a_worker_that_lost_its_lease_records_no_call(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    await start_check(engine, clock)
    late = await queue.claim("replica-a/0")
    assert late is not None
    clock.advance(LEASE + 1)
    assert await queue.claim("replica-b/0") is not None
    call = Call(role="supervisor", requested_model="m", served_model="m", effort="high",
                stop_reason="end_turn", fallback=False, usage=Usage(input_tokens=1),
                cost_usd=Decimal(0))  # fmt: skip
    with pytest.raises(LeaseLostError):
        await queue.record_call(late, call)


# --- The investigation graph (M8.4)


async def start_investigation(
    engine: AsyncEngine, clock: Clock | None = None, key: str = "graph-key-1", **parameters: str
) -> pb.Run:
    request = pb.StartRunRequest(
        case_id=CASE, snapshot_id=SNAPSHOT, workflow="investigation",
        parameters={"team": "false", **parameters},
        requested_by="service:investigation", idempotency_key=key,
    )  # fmt: skip
    with caller():
        api = AgentRuntimeService(
            engine, REGISTRY, peer=lambda: INVESTIGATION, clock=clock or Clock()
        )
        return (await api.StartRun(request, None)).run


async def rows_of(engine: AsyncEngine, table: Any, run_id: str) -> list[Any]:
    async with engine.connect() as connection:
        return list((await connection.execute(select(table).where(table.c.run_id == run_id))).all())


async def wait_until(test: Callable[[], Awaitable[bool]], seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.02)):
        if await test():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out")


def test_the_graph_has_srs_steps_in_order_and_no_interrupt() -> None:
    assert [name for name, _ in STEPS] == [
        "receive", "validate", "snapshot", "plan", "specialists", "documents", "research",
        "impact", "aggregate", "validate_output", "policy_checks", "recommend", "conclude",
    ]  # fmt: skip
    graph = build(None, {"step_seconds": 0, "plan_with_claude": False}).compile()  # type: ignore[arg-type]
    # AGT-003: a human decision never waits inside the graph.
    assert not graph.interrupt_before_nodes and not graph.interrupt_after_nodes


@scenario
async def test_an_investigation_runs_every_step_and_checkpoints_each(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_investigation(engine, clock)
    assert json.loads(run.pins_json)["graph"] == "investigation@3"
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    assert await Worker(queue, REGISTRY).execute(claim, asyncio.Event()) == "completed"
    done = await steps(engine, run.run_id)
    assert [s.name for s in done] == [name for name, _ in STEPS]
    checkpoints = await rows_of(engine, RUN_CHECKPOINTS, run.run_id)
    assert len(checkpoints) >= len(STEPS)


@scenario
async def test_a_run_taken_over_mid_graph_carries_on_from_its_newest_checkpoint(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_investigation(engine, clock, step_seconds="0.05", plan_with_claude="true")
    first = await queue.claim("replica-a/0")
    assert first is not None
    stalled = asyncio.create_task(claude_worker(queue).execute(first, asyncio.Event()))

    async def past_the_plan() -> bool:
        return len(await steps(engine, run.run_id)) >= 6

    await wait_until(past_the_plan)
    # Its lease runs out while it is still working, as a frozen or cut-off worker's would.
    clock.advance(LEASE + 1)
    second = await queue.claim("replica-b/0")
    assert second is not None and second.reclaimed and second.attempt == 2
    # The first worker's next write is refused, so it stops. It is awaited first only because
    # SQLite here is one shared connection: on PostgreSQL each worker has its own.
    assert await asyncio.wait_for(stalled, timeout=5) == "lost"
    assert await claude_worker(queue).execute(second, asyncio.Event()) == "completed"
    done = await steps(engine, run.run_id)
    assert [s.name for s in sorted(done, key=lambda s: s.position)] == [n for n, _ in STEPS]
    taken_over = second.completed
    assert {s.attempt for s in done if s.position in taken_over} == {1}
    assert {s.attempt for s in done if s.position not in taken_over} == {2}
    assert len(taken_over) >= 6
    # The Supervisor's plan was asked for once: the resumed run read it from the checkpoint.
    calls = await rows_of(engine, RUN_CALLS, run.run_id)
    assert len(calls) == 1 and calls[0].attempt == 1
    checkpoints = await rows_of(engine, RUN_CHECKPOINTS, run.run_id)
    by_id = {c.checkpoint_id: c for c in checkpoints}
    resumed = min((c for c in checkpoints if c.attempt == 2), key=lambda c: c.checkpoint_id)
    assert by_id[resumed.parent_id].attempt == 1


@scenario
async def test_a_paused_run_keeps_its_checkpoints_and_resumes_where_it_stopped(
    engine: AsyncEngine,
) -> None:
    live = Queue(engine, lease_seconds=0.3, queue_limit=timedelta(hours=24))
    # Queued now, as the live queue's clock is real: a fixed date would age past its limit.
    now = Clock()
    now.now = datetime.now(UTC)
    run = await start_investigation(engine, now, step_seconds="0.1")
    claim = await live.claim("replica-a/0")
    assert claim is not None
    working = asyncio.create_task(Worker(live, REGISTRY).execute(claim, asyncio.Event()))

    async def started() -> bool:
        return len(await steps(engine, run.run_id)) >= 3

    await wait_until(started)
    api = AgentRuntimeService(engine, REGISTRY, peer=lambda: INVESTIGATION)
    with caller():
        asked = (await api.PauseRun(pb.PauseRunRequest(
            run_id=run.run_id, requested_by="user:engineer-1", reason="Waiting on a site visit",
        ), None)).run  # fmt: skip
    assert asked.status == "running" and asked.pause_requested
    assert await asyncio.wait_for(working, timeout=5) == "paused"
    paused = await get(engine, Clock(), run.run_id)
    assert (paused.status, paused.attempt) == ("paused", 0)
    held = len(await steps(engine, run.run_id))
    assert await live.claim("replica-b/0") is None
    with caller():
        resumed = (await api.ResumeRun(pb.ResumeRunRequest(
            run_id=run.run_id, requested_by="user:engineer-1"), None)).run  # fmt: skip
    assert resumed.status == "queued" and not resumed.pause_requested
    again = await live.claim("replica-b/0")
    assert again is not None and again.attempt == 1 and len(again.completed) == held
    assert await Worker(live, REGISTRY).execute(again, asyncio.Event()) == "completed"
    done = await steps(engine, run.run_id)
    assert sorted(s.position for s in done) == list(range(1, len(STEPS) + 1))
    assert (await audits(engine))[-3:-1] == ["agent_run.pause_requested", "agent_run.resumed"]


@scenario
async def test_a_queued_run_pauses_at_once_and_a_paused_run_can_be_cancelled(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    run = await start_investigation(engine, clock)
    api = service(engine, clock)
    with caller():
        hold = pb.PauseRunRequest(run_id=run.run_id, requested_by="user:engineer-1", reason="Hold")
        paused = (await api.PauseRun(hold, None)).run
        assert paused.status == "paused"
        assert await queue.claim("replica-a/0") is None
        stop = pb.CancelRunRequest(
            run_id=run.run_id, requested_by="user:engineer-1", reason="Not needed"
        )
        cancelled = (await api.CancelRun(stop, None)).run
    assert cancelled.status == "cancelled"


@scenario
async def test_a_run_never_resumes_on_other_pins(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    @dataclasses.dataclass(frozen=True)
    class Reshaped(Investigation):
        def pins(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
            return {**super().pins(parameters), "graph_shape": "another"}

    run = await start_investigation(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    worker = Worker(queue, {"investigation": Reshaped()})
    assert await worker.execute(claim, asyncio.Event()) == "stale"
    assert "versions changed" in (await get(engine, clock, run.run_id)).reason


@scenario
async def test_a_worker_that_lost_its_lease_writes_no_checkpoint(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    run = await start_investigation(engine, clock)
    late = await queue.claim("replica-a/0")
    assert late is not None
    clock.advance(LEASE + 1)
    assert await queue.claim("replica-b/0") is not None
    config: Any = {"configurable": {"thread_id": run.run_id, "checkpoint_ns": ""}}
    with pytest.raises(LeaseLostError):
        await RunCheckpointer(queue, late).aput(config, empty_checkpoint(), {}, {})
    assert not await rows_of(engine, RUN_CHECKPOINTS, run.run_id)


# --- Agent identities (M8.5)

DELEGATION = "dlg_" + "c" * 20


class FakeIdentity:
    """Stands in for the Identity Service, which has its own tests and cluster check."""

    def __init__(self) -> None:
        self.issued: list[tuple[str, str, str, str, tuple[str, ...]]] = []
        self.correlations: list[str] = []
        self.ended: list[tuple[str, str, str]] = []

    async def agent_token(
        self,
        tenant: str,
        delegation: str,
        agent: str,
        audience: str,
        scopes: Sequence[str],
        correlation_id: str = "",
    ) -> str:
        self.issued.append((tenant, delegation, agent, audience, tuple(scopes)))
        self.correlations.append(correlation_id)
        return "agent-token"

    async def end(self, tenant: str, delegation: str, reason: str) -> None:
        self.ended.append((tenant, delegation, reason))


@dataclasses.dataclass(frozen=True)
class AsksForAToken(Rehearsal):
    async def run(self, context: Any, parameters: Mapping[str, Any]) -> None:
        assert context.tokens is not None
        token = await context.tokens.for_tool(
            "reliability-engineer", "windops-mcp-documentation",
            ["mcp.documentation.search_documents"],
        )  # fmt: skip
        assert token == "agent-token"


@scenario
async def test_a_runs_agents_get_tokens_under_its_delegation_which_ends_with_it(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    request = start_request()
    request.delegation_id = DELEGATION
    with caller():
        run = (await service(engine, clock).StartRun(request, None)).run
    assert run.delegation_id == DELEGATION
    claim = await queue.claim("replica-a/0")
    assert claim is not None and claim.delegation_id == DELEGATION
    identity = FakeIdentity()
    worker = Worker(queue, {"rehearsal": AsksForAToken()}, identity=identity)
    assert await worker.execute(claim, asyncio.Event()) == "completed"
    assert identity.issued == [(
        TENANT, DELEGATION, "reliability-engineer", "windops-mcp-documentation",
        ("mcp.documentation.search_documents",),
    )]  # fmt: skip
    assert identity.ended == [(TENANT, DELEGATION, "The run completed")]
    # Each token carries the run's ID, so its tool call's audit record shares the trace's (OBS-008).
    assert identity.correlations == [run.run_id]


@scenario
async def test_a_run_without_a_delegation_gets_no_tokens(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    await start(engine, clock)
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    identity = FakeIdentity()
    worker = Worker(queue, {"rehearsal": AsksForAToken()}, identity=identity)
    # NoDelegationError fails the attempt; nothing was asked of the Identity Service.
    assert await worker.execute(claim, asyncio.Event()) == "retried"
    assert identity.issued == [] and identity.ended == []


def test_a_bad_delegation_id_is_refused() -> None:
    async def body() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        request = start_request()
        request.delegation_id = "delegation-1"
        with caller(), pytest.raises(InvalidRequestError, match="delegation_id"):
            await AgentRuntimeService(engine, REGISTRY, peer=lambda: INVESTIGATION).StartRun(
                request, None
            )
        await engine.dispose()

    asyncio.run(body())


@scenario
async def test_an_investigation_with_its_team_keeps_a_checked_result_and_its_policy_outcome(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    """A rehearsal: the real graph and team on a scripted model, the result checked, and every
    recommended action through policy (M8.7, M8.8)."""
    from team_support import Tokens
    from windops_agent_runtime.team.definitions import team
    from windops_agent_runtime.team.run import TeamRunner
    from windops_agent_runtime.team.tools import ToolBox, stand_in

    async def answer(arguments: dict[str, Any]) -> str:
        return "{}"

    async def evidence(arguments: dict[str, Any]) -> str:
        return json.dumps({"snapshot_id": SNAPSHOT, "items": [{"record_id": "tel_1"}]})

    async def policy(arguments: dict[str, Any]) -> str:
        return json.dumps({"result_id": "pol_1", "outcome": "escalate", "rules": []})

    every: dict[str, Any] = {
        f"{scope.split('.')[1]}_{scope.split('.')[2]}": answer
        for agent in team().agents.values() for scope in agent.tools
    }  # fmt: skip
    every |= {"windops_get_evidence": evidence, "windops_check_policy": policy}

    def make_team(claim: Any, claude: Any, tokens: Any) -> TeamRunner:
        return TeamRunner(team(), ToolBox(stand_in(every)), claude=claude, tokens=Tokens(),
                          tool_calls=50)  # fmt: skip

    request = pb.StartRunRequest(
        case_id=CASE, snapshot_id=SNAPSHOT, workflow="investigation",
        parameters={"team": "true", "rehearse": "true", "turbine_id": "dogger-east-t01"},
        requested_by="service:investigation", idempotency_key="team-key-1",
        delegation_id="dlg_" + "c" * 20,
    )  # fmt: skip
    with caller():
        run = (await service(engine, clock).StartRun(request, None)).run
    assert set(json.loads(run.pins_json)["prompts"]) == {*team().agents, "supervisor:result"}
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    worker = Worker(
        queue, REGISTRY, policy=POLICY, identity=FakeIdentity(), make_team=make_team,
        make_claude=lambda claim, meter: Claude(POLICY, meter, lambda: "key"),
    )  # fmt: skip
    assert await worker.execute(claim, asyncio.Event()) == "completed"
    kept = json.loads((await get(engine, clock, run.run_id)).result_json)
    assert (kept["case_id"], kept["snapshot_id"]) == (CASE, SNAPSHOT)
    assert kept["observed_facts"][0]["citations"] == ["tel_1"]
    # Policy escalated the recommended inspection, so a person must look.
    assert kept["policy"] == [{"action": "inspection", "result_id": "pol_1", "outcome": "escalate"}]
    assert kept["policy_result_id"] == "pol_1" and kept["escalation_required"] is True
    assert kept["model_versions"] == {} and kept["repairs"] == 0


class Captured:
    """What a LangSmith client would send, after its redaction hooks: nothing leaves the test."""

    def __init__(self) -> None:
        from langsmith import Client
        from windops_observability import redact_trace

        sent: list[dict[str, Any]] = []
        self.sent = sent

        class Capturing(Client):
            def _create_run(self, run_create: dict[str, Any], **kwargs: Any) -> None:
                sent.append(run_create)

            def _update_run(self, run_update: dict[str, Any], **kwargs: Any) -> None:
                sent.append(run_update)

        self.client = Capturing(
            api_key="unused", api_url="http://127.0.0.1:9", auto_batch_tracing=False,
            hide_inputs=redact_trace, hide_outputs=redact_trace, hide_metadata=redact_trace,
        )  # fmt: skip


@scenario
async def test_a_traced_run_reaches_langsmith_only_redacted_with_its_ids(
    engine: AsyncEngine, clock: Clock, queue: Queue
) -> None:
    """EGR-003, OBS-005, OBS-008: a rehearsal traced, with a token and an e-mail planted."""
    from team_support import Tokens
    from windops_agent_runtime.team.definitions import team
    from windops_agent_runtime.team.run import TeamRunner
    from windops_agent_runtime.team.tools import ToolBox, stand_in
    from windops_agent_runtime.tracing import Tracer

    jwt_ = "eyJhbGciOiJFUzI1NiJ9" + "." + "eyJzdWIiOiJ1LTEifQ" + "." + "c2lnbmF0dXJl"
    email = "operator" + "@" + "north-sea.example"

    async def answer(arguments: dict[str, Any]) -> str:
        return "{}"

    async def evidence(arguments: dict[str, Any]) -> str:
        planted = f"Contact {email}, Authorization: Bearer {jwt_}"
        item = {"record_id": "tel_1", "content_json": planted}
        return json.dumps({"snapshot_id": SNAPSHOT, "items": [item]})

    async def policy(arguments: dict[str, Any]) -> str:
        return json.dumps({"result_id": "pol_1", "outcome": "escalate", "rules": []})

    every: dict[str, Any] = {
        f"{scope.split('.')[1]}_{scope.split('.')[2]}": answer
        for agent in team().agents.values() for scope in agent.tools
    }  # fmt: skip
    every |= {"windops_get_evidence": evidence, "windops_check_policy": policy}
    captured = Captured()
    request = pb.StartRunRequest(
        case_id=CASE, snapshot_id=SNAPSHOT, workflow="investigation",
        parameters={"team": "true", "rehearse": "true", "turbine_id": "dogger-east-t01"},
        requested_by="user:engineer-1", idempotency_key="trace-key-1",
        delegation_id="dlg_" + "c" * 20,
    )  # fmt: skip
    with caller():
        run = (await service(engine, clock).StartRun(request, None)).run
    claim = await queue.claim("replica-a/0")
    assert claim is not None
    worker = Worker(
        queue, REGISTRY, policy=POLICY, identity=FakeIdentity(),
        make_team=lambda claim, claude, tokens: TeamRunner(
            team(), ToolBox(stand_in(every)), claude=claude, tokens=Tokens(), tool_calls=50
        ),
        make_claude=lambda claim, meter: Claude(POLICY, meter, lambda: "key"),
        tracer=Tracer(captured.client, "windops-test"),
    )  # fmt: skip
    assert await worker.execute(claim, asyncio.Event()) == "completed"
    text = json.dumps(captured.sent, default=str)
    assert jwt_ not in text and email not in text and "[REDACTED]" in text
    roots = [r for r in captured.sent if r.get("name") == "investigation"]
    metadata = roots[0]["extra"]["metadata"]
    assert (metadata["run_id"], metadata["case_id"], metadata["tenant_id"]) == (
        run.run_id, CASE, TENANT,
    )  # fmt: skip
    assert (
        metadata["correlation_id"] == run.run_id and metadata["requested_by"] == "user:engineer-1"
    )
    names = {r.get("name") for r in captured.sent}
    # The tree holds the graph's steps, the team, and every agent's tool calls.
    assert {"specialists", "validate_output", "policy_checks", "windops_get_evidence",
            "windops_get_turbine", "weather_get_forecast"} <= names  # fmt: skip
