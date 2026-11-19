"""The worker: each replica runs a few slots, each claiming and running one run at a time (ARC-006).

While a run works, a heartbeat renews its lease. The heartbeat also stops the work when the run is
cancelled, or when the lease turns out to be lost. Shutting down hands each run back at once, so
another replica need not wait for the lease to run out.
"""

import asyncio
import contextlib
import logging
import random
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from opentelemetry import metrics
from sqlalchemy.exc import IntegrityError
from windops_claude import (
    Budget,
    BudgetExhaustedError,
    Call,
    Claude,
    HistoryEditedError,
    Meter,
    Policy,
    Spent,
    StopError,
    StructuredOutputError,
)

from windops_agent_runtime.checkpoints import RunCheckpointer
from windops_agent_runtime.identity import AgentTokens, Identity
from windops_agent_runtime.investigation import canonical_pins
from windops_agent_runtime.queue import Claim, LeaseLostError, Queue
from windops_agent_runtime.store import BUDGET_EXHAUSTED, CANCELLED, COMPLETED, FAILED, STALE
from windops_agent_runtime.team.compose import InvalidResultError
from windops_agent_runtime.team.definitions import team
from windops_agent_runtime.team.run import TeamRunner
from windops_agent_runtime.team.sandbox import SandboxRun, SandboxUse
from windops_agent_runtime.team.tools import ToolBox
from windops_agent_runtime.tracing import Tracer, run_metadata
from windops_agent_runtime.workflows import ClaudeUnavailableError, StepFailedError, Workflow

logger = logging.getLogger("windops.agent_runtime")


# Errors another attempt would only repeat: a refusal after fallback, a cut-off or malformed
# answer, an edited history, no key at all, or a result still wrong after its repairs.
FINAL = (
    StopError, StructuredOutputError, HistoryEditedError, ClaudeUnavailableError,
    InvalidResultError,
)  # fmt: skip
_meter = metrics.get_meter("windops.agent_runtime")
_claims = _meter.create_counter(
    "windops_agent_run_claims", description="Runs claimed, and whether each was taken over"
)
_ended = _meter.create_counter("windops_agent_runs_ended", description="Runs ended, by state")
_lost = _meter.create_counter(
    "windops_agent_run_leases_lost", description="Runs a worker stopped because its lease was lost"
)


def _note_lost(claim: Claim) -> None:
    _lost.add(1)
    logger.warning(
        "Lost a run's lease, so another replica has it: stopping",
        extra={"run_id": claim.run_id, "attempt": claim.attempt},
    )


@dataclass(frozen=True)
class WorkerSettings:
    slots: int = 2
    poll_seconds: float = 1.0
    retry_seconds: float = 5.0


class RunLedger:
    """A run's calls to Claude, kept with the run, over every attempt (LLM-010)."""

    def __init__(self, queue: Queue, claim: Claim) -> None:
        self._queue, self._claim = queue, claim

    async def spent(self) -> Spent:
        return await self._queue.spent(self._claim)

    async def record(self, call: Call) -> Spent:
        return await self._queue.record_call(self._claim, call)


class _Steps:
    """The workflow's context: counts steps, skips those already done, and records each one.
    Its Claude client meters every call against the run's budget."""

    def __init__(
        self,
        queue: Queue,
        claim: Claim,
        claude: Claude | None,
        tokens: AgentTokens | None = None,
        team: TeamRunner | None = None,
    ) -> None:
        self._queue, self._claim, self._position = queue, claim, 0
        self.claude = claude
        # Its agents' tool tokens, under the run's delegation (ADR 0019).
        self.tokens = tokens
        # The investigation team, on this run's budget and delegation (M8.7).
        self.team = team
        # What the run's trace root says about it (OBS-005, OBS-008).
        self.trace_metadata: dict[str, Any] = {}
        self.checkpointer: BaseCheckpointSaver[str] | None = RunCheckpointer(queue, claim)
        self.run_id, self.case_id = claim.run_id, claim.case_id
        self.snapshot_id = claim.snapshot_id

    async def models_used(self) -> dict[str, list[str]]:
        return await self._queue.models_used(self._claim)

    async def save_result(self, result: dict[str, Any]) -> None:
        await self._queue.save_result(self._claim, result)

    async def record(self, position: int, name: str, started_at: datetime) -> None:
        if position in self._claim.completed:
            return
        try:
            await self._queue.complete_step(
                self._claim, position=position, name=name, started_at=started_at
            )
        except IntegrityError:
            # Recorded by an attempt that stopped before its checkpoint: the node ran again.
            return

    async def step(self, name: str, work: Callable[[], Awaitable[None]]) -> None:
        self._position += 1
        if self._position in self._claim.completed:
            return
        started = datetime.now(UTC)
        await work()
        await self._queue.complete_step(
            self._claim, position=self._position, name=name, started_at=started
        )


class Worker:
    def __init__(
        self,
        queue: Queue,
        workflows: Mapping[str, Workflow],
        settings: WorkerSettings | None = None,
        *,
        name: str | None = None,
        policy: Policy | None = None,
        api_key: Callable[[], str] | None = None,
        make_claude: Callable[[Claim, Meter], Claude] | None = None,
        identity: Identity | None = None,
        toolbox: ToolBox | None = None,
        make_team: Callable[[Claim, Claude, AgentTokens], TeamRunner] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._queue, self._workflows = queue, workflows
        self._policy = policy
        self._api_key = api_key
        self._make_claude = make_claude
        self._identity = identity
        self._toolbox = toolbox
        self._make_team = make_team
        self._tracer = tracer
        self._settings = settings or WorkerSettings()
        self._name = name or socket.gethostname()

    async def run_forever(self, stop: asyncio.Event) -> None:
        slots = [
            asyncio.create_task(self._slot(stop, f"{self._name}/{index}"))
            for index in range(self._settings.slots)
        ]
        await asyncio.gather(*slots)

    async def _slot(self, stop: asyncio.Event, owner: str) -> None:
        while not stop.is_set():
            claim = None
            try:
                claim = await self._queue.claim(owner)
            except Exception as error:  # The database is down: the runs wait, and so do we.
                logger.warning("Cannot claim a run yet", extra={"error_type": type(error).__name__})
            if claim is None:
                # Jittered, so replicas do not all ask at once.
                pause = self._settings.poll_seconds * random.uniform(0.5, 1.5)  # noqa: S311
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=pause)
                continue
            _claims.add(1, {"reclaimed": claim.reclaimed})
            logger.info(
                "Claimed a run",
                extra={"run_id": claim.run_id, "attempt": claim.attempt,
                       "reclaimed": claim.reclaimed, "worker": owner},
            )  # fmt: skip
            await self.execute(claim, stop)

    async def execute(self, claim: Claim, stop: asyncio.Event) -> str:
        """Run the claim to an end. Returns what happened, for logs and tests."""
        workflow = self._workflows.get(claim.workflow)
        if workflow is None or workflow.version != claim.workflow_version:
            # A run never continues on a workflow other than the one it was queued with (AGT-005).
            return await self._end(
                claim, STALE, "Its workflow version is no longer available: start it again"
            )
        if canonical_pins(workflow.pins(claim.parameters)) != canonical_pins(claim.pins or {}):
            # Nor on a graph, prompt, tool or skill version other than its own (AGT-004, AGT-005).
            return await self._end(
                claim, STALE, "Its graph, prompt, tool or skill versions changed: start it again"
            )
        if (
            self._policy is not None
            and claim.policy_hash
            and (claim.policy_hash != self._policy.content_hash)
        ):
            # Nor under a Claude policy other than its own (AGT-004, AGT-005).
            return await self._end(
                claim, STALE, "Its Claude policy version is no longer in force: start it again"
            )
        cancelled, lost, paused = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tokens = (
            AgentTokens(self._identity, claim.tenant_id, claim.delegation_id, claim.run_id)
            if self._identity is not None
            else None
        )
        claude = self._claude_for(claim)
        context = _Steps(self._queue, claim, claude, tokens, self._team_for(claim, claude, tokens))
        context.trace_metadata = run_metadata(
            run_id=claim.run_id, case_id=claim.case_id, snapshot_id=claim.snapshot_id,
            tenant_id=claim.tenant_id, requested_by=claim.requested_by,
            workflow=f"{claim.workflow}@{claim.workflow_version}", attempt=claim.attempt,
        )  # fmt: skip
        work = asyncio.create_task(self._traced(claim, workflow.run(context, claim.parameters)))
        beat = asyncio.create_task(self._heartbeat(claim, work, cancelled, lost, paused))
        shutdown = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({work, shutdown}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            beat.cancel()
            shutdown.cancel()
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            if lost.is_set():
                return "lost"
            await self._queue.release(claim)
            logger.info("Handed a run back", extra={"run_id": claim.run_id})
            return "released"
        return await self._settle(claim, work, cancelled, lost, paused)

    async def _traced(self, claim: Claim, run: Awaitable[None]) -> None:
        """The run, traced to LangSmith through the redacting client, when tracing is on."""
        if self._tracer is None:
            await run
            return
        try:
            with self._tracer.tracing(claim.run_id, tags=[claim.workflow, claim.tenant_id]):
                await run
        finally:
            # In a thread: a slow LangSmith must never hold a worker or its lease up.
            await asyncio.to_thread(self._tracer.flush)

    def _team_for(
        self, claim: Claim, claude: Claude | None, tokens: AgentTokens | None
    ) -> TeamRunner | None:
        """The team runs only with Claude, under the run's budget, and with agent tokens."""
        if claude is None or tokens is None:
            return None
        if self._make_team is not None:
            return self._make_team(claim, claude, tokens)
        if self._toolbox is None or self._policy is None:
            return None
        queue = self._queue

        async def record(use: SandboxUse) -> None:
            await queue.record_sandbox(claim, use)

        return TeamRunner(
            team(), self._toolbox, claude=claude, tokens=tokens,
            tool_calls=self._policy.budget_for(claim.tenant_id).tool_calls,
            sandbox_run=SandboxRun(claim.run_id, claim.tenant_id, record),
        )  # fmt: skip

    def _claude_for(self, claim: Claim) -> Claude | None:
        if self._policy is None or not claim.token_budget:
            return None
        budget = Budget(tokens=claim.token_budget, cost_usd=claim.cost_budget_usd)
        meter = Meter(budget, RunLedger(self._queue, claim))
        if self._make_claude is not None:
            return self._make_claude(claim, meter)
        if self._api_key is None:
            return None
        return Claude(self._policy, meter, self._api_key)

    async def _settle(
        self,
        claim: Claim,
        work: "asyncio.Task[None]",
        cancelled: asyncio.Event,
        lost: asyncio.Event,
        paused: asyncio.Event,
    ) -> str:
        if lost.is_set():
            return "lost"
        if cancelled.is_set():
            return await self._end(claim, CANCELLED, "Cancelled")
        if paused.is_set():
            # Its newest checkpoint stays: resumed, the run carries on from there.
            if await self._queue.pause(claim):
                logger.info("Paused a run", extra={"run_id": claim.run_id})
                return "paused"
            _note_lost(claim)
            return "lost"
        if work.cancelled():
            return "lost"
        error = work.exception()
        if isinstance(error, LeaseLostError):
            _note_lost(claim)
            return "lost"
        if error is None:
            return await self._end(claim, COMPLETED, "")
        if isinstance(error, BudgetExhaustedError):
            # LLM-010. WindopsAgentBudgetExhausted alerts a person, until Notification (M10.5).
            return await self._end(claim, BUDGET_EXHAUSTED, str(error))
        if isinstance(error, FINAL):
            # Trying again would get the same answer, and pay for it again.
            return await self._end(claim, FAILED, f"{type(error).__name__}: {error}")
        reason = str(error) if isinstance(error, StepFailedError) else type(error).__name__
        if not isinstance(error, StepFailedError):
            logger.warning(
                "A run's attempt failed",
                extra={"run_id": claim.run_id, "error_type": type(error).__name__},
            )
        await self._queue.retry_later(claim, reason, self._settings.retry_seconds * claim.attempt)
        return "retried" if claim.attempt < claim.max_attempts else "failed"

    async def _end(self, claim: Claim, status: str, reason: str) -> str:
        if await self._queue.finish(claim, status, reason):
            _ended.add(1, {"status": status})
            logger.info("Ended a run", extra={"run_id": claim.run_id, "status": status})
            await self._end_delegation(claim, status)
            return status
        _note_lost(claim)
        return "lost"

    async def _end_delegation(self, claim: Claim, status: str) -> None:
        """No agent acts for anyone once the run is over. If the Identity Service cannot be
        reached, the delegation still expires on its own."""
        if self._identity is None or not claim.delegation_id:
            return
        try:
            await self._identity.end(claim.tenant_id, claim.delegation_id, f"The run {status}")
        except Exception as error:
            logger.warning(
                "Could not end a run's delegation",
                extra={"run_id": claim.run_id, "error_type": type(error).__name__},
            )

    async def _heartbeat(
        self,
        claim: Claim,
        work: "asyncio.Task[None]",
        cancelled: asyncio.Event,
        lost: asyncio.Event,
        paused: asyncio.Event,
    ) -> None:
        every = self._queue.lease_seconds / 3
        while not work.done():
            await asyncio.sleep(every)
            try:
                renewal = await self._queue.renew(claim)
            except Exception as error:
                # Keep working: the lease has time left, and the next beat may get through.
                logger.warning("Cannot renew a lease", extra={"error_type": type(error).__name__})
                continue
            if not renewal.held:
                _note_lost(claim)
                lost.set()
                work.cancel()
                return
            if renewal.cancel_requested:
                cancelled.set()
                work.cancel()
                return
            if renewal.pause_requested:
                paused.set()
                work.cancel()
                return
