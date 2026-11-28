"""A bulkhead: a limit on calls in flight to one dependency (RES-002).

A slow dependency holds every call made to it until its deadline. Without a limit, those calls
pile up until they hold all of a service's workers, and calls to healthy dependencies wait
behind them. A bulkhead lets a few callers queue briefly for a slot, and refuses the rest at
once, so the slow dependency's share of the workers stays bounded.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from windops_resilience.errors import BreakerOpenError

logger = logging.getLogger("windops.resilience")


class BulkheadFullError(BreakerOpenError):
    """The dependency's bulkhead was full, so the call was refused without reaching it.

    It is a BreakerOpenError, so each caller's SRS 7.7 fallback for that dependency applies.
    """

    def __init__(self, dependency: str) -> None:
        Exception.__init__(self, f"Bulkhead for {dependency} is full")
        self.dependency = dependency


@dataclass(frozen=True)
class BulkheadConfig:
    """How many calls may be in flight, and how many may wait, for one dependency.

    The defaults keep one dependency to at most 48 of a service's 128 gRPC workers
    (GRPC_MAX_CONCURRENT_RPCS), with room left for every other dependency.
    """

    max_concurrent: int = 32
    max_waiting: int = 16
    max_wait_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if self.max_waiting < 0:
            raise ValueError("max_waiting must not be negative")
        if self.max_wait_seconds < 0:
            raise ValueError("max_wait_seconds must not be negative")


BulkheadFullHook = Callable[[str], None]


class Bulkhead:
    """Admits at most `max_concurrent` calls to one dependency, on a single event loop.

    A freed slot passes straight to the longest waiter, so waiters are served in order.
    """

    def __init__(
        self,
        dependency: str,
        config: BulkheadConfig,
        *,
        on_full: BulkheadFullHook | None = None,
    ) -> None:
        self.dependency = dependency
        self._config = config
        self._on_full = on_full
        self._in_flight = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._refusing = False

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self) -> None:
        """Take a slot, waiting a short while for one, or raise BulkheadFullError."""
        if self._in_flight < self._config.max_concurrent and not self._waiters:
            self._in_flight += 1
            self._refusing = False
            return
        if len(self._waiters) >= self._config.max_waiting or self._config.max_wait_seconds == 0:
            self._refuse()
        slot: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(slot)
        try:
            async with asyncio.timeout(self._config.max_wait_seconds):
                await slot
        except TimeoutError:
            if not self._handed_over(slot):
                self._refuse()
        except asyncio.CancelledError:
            if self._handed_over(slot):
                self.release()
            raise

    def release(self) -> None:
        """Give a slot back, to the longest waiter if there is one."""
        while self._waiters:
            slot = self._waiters.popleft()
            if not slot.done():
                slot.set_result(None)
                return
        self._in_flight -= 1

    def _handed_over(self, slot: asyncio.Future[None]) -> bool:
        """Whether a slot reached this waiter as it gave up, so it now holds one."""
        with_slot = slot.done() and not slot.cancelled()
        if not with_slot and slot in self._waiters:
            self._waiters.remove(slot)
        return with_slot

    def _refuse(self) -> None:
        if not self._refusing:
            # Once per episode, not per call, or a flood of refusals floods the logs too.
            logger.warning(
                "Bulkhead is full, refusing calls",
                extra={
                    "dependency": self.dependency,
                    "max_concurrent": self._config.max_concurrent,
                    "max_waiting": self._config.max_waiting,
                },
            )
            self._refusing = True
        if self._on_full is not None:
            self._on_full(self.dependency)
        raise BulkheadFullError(self.dependency)
