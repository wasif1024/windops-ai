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

from opentelemetry import metrics

from windops_agent_runtime.queue import Claim, LeaseLostError, Queue
from windops_agent_runtime.store import CANCELLED, COMPLETED, STALE
from windops_agent_runtime.workflows import StepFailedError, Workflow

logger = logging.getLogger("windops.agent_runtime")
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


class _Steps:
    """The workflow's context: counts steps, skips those already done, and records each one."""

    def __init__(self, queue: Queue, claim: Claim) -> None:
        self._queue, self._claim, self._position = queue, claim, 0

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
    ) -> None:
        self._queue, self._workflows = queue, workflows
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
        cancelled, lost = asyncio.Event(), asyncio.Event()
        work = asyncio.create_task(workflow.run(_Steps(self._queue, claim), claim.parameters))
        beat = asyncio.create_task(self._heartbeat(claim, work, cancelled, lost))
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
        return await self._settle(claim, work, cancelled, lost)

    async def _settle(
        self,
        claim: Claim,
        work: "asyncio.Task[None]",
        cancelled: asyncio.Event,
        lost: asyncio.Event,
    ) -> str:
        if lost.is_set():
            return "lost"
        if cancelled.is_set():
            return await self._end(claim, CANCELLED, "Cancelled")
        if work.cancelled():
            return "lost"
        error = work.exception()
        if isinstance(error, LeaseLostError):
            _note_lost(claim)
            return "lost"
        if error is None:
            return await self._end(claim, COMPLETED, "")
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
            return status
        _note_lost(claim)
        return "lost"

    async def _heartbeat(
        self,
        claim: Claim,
        work: "asyncio.Task[None]",
        cancelled: asyncio.Event,
        lost: asyncio.Event,
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
