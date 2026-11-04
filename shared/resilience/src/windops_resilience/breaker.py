"""A circuit breaker with the SRS section 7.7 starting defaults."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from windops_resilience.classify import is_dependency_failure

logger = logging.getLogger("windops.resilience")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerOpenError(Exception):
    """The call was refused without reaching the dependency."""

    def __init__(self, dependency: str) -> None:
        super().__init__(f"Circuit breaker for {dependency} is open")
        self.dependency = dependency


@dataclass(frozen=True)
class BreakerConfig:
    """Breaker settings. The SRS sets no slow-call limit, so each dependency must choose one."""

    slow_call_seconds: float
    window_size: int = 20
    minimum_calls: int = 10
    failure_rate: float = 0.5
    open_seconds: float = 30.0
    half_open_probes: int = 3

    def __post_init__(self) -> None:
        if self.slow_call_seconds <= 0 or self.open_seconds <= 0:
            raise ValueError("Breaker durations must be positive")
        if not 0 < self.minimum_calls <= self.window_size:
            raise ValueError("minimum_calls must be between 1 and window_size")
        if not 0 < self.failure_rate <= 1:
            raise ValueError("failure_rate must be above 0 and at most 1")
        if self.half_open_probes < 1:
            raise ValueError("half_open_probes must be at least 1")


StateChangeHook = Callable[[str, BreakerState, BreakerState], None]


class CircuitBreaker:
    """Protects one dependency for one calling service (SRS section 7.7).

    Use one instance per dependency in each service, on a single event loop.
    """

    def __init__(
        self,
        dependency: str,
        config: BreakerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: StateChangeHook | None = None,
    ) -> None:
        self.dependency = dependency
        self._config = config
        self._clock = clock
        self._on_state_change = on_state_change
        self._state = BreakerState.CLOSED
        self._outcomes: deque[bool] = deque(maxlen=config.window_size)
        self._opened_at = 0.0
        self._probes_started = 0
        self._probes_succeeded = 0
        # Results from calls started before the latest state change are ignored.
        self._generation = 0

    @property
    def state(self) -> BreakerState:
        if (
            self._state is BreakerState.OPEN
            and self._clock() - self._opened_at >= self._config.open_seconds
        ):
            self._transition(BreakerState.HALF_OPEN)
        return self._state

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run an operation through the breaker, or raise BreakerOpenError."""
        generation = self._acquire()
        started = self._clock()
        try:
            result = await operation()
        except asyncio.CancelledError:
            self._release(generation)
            raise
        except Exception as error:
            healthy = not is_dependency_failure(error) and not self._is_slow(started)
            self._record(generation, healthy=healthy)
            raise
        self._record(generation, healthy=not self._is_slow(started))
        return result

    def _is_slow(self, started: float) -> bool:
        return self._clock() - started >= self._config.slow_call_seconds

    def _acquire(self) -> int:
        state = self.state
        if state is BreakerState.OPEN:
            raise BreakerOpenError(self.dependency)
        if state is BreakerState.HALF_OPEN:
            if self._probes_started >= self._config.half_open_probes:
                raise BreakerOpenError(self.dependency)
            self._probes_started += 1
        return self._generation

    def _release(self, generation: int) -> None:
        """Forget a cancelled call, which says nothing about the dependency's health."""
        if generation == self._generation and self._state is BreakerState.HALF_OPEN:
            self._probes_started -= 1

    def _record(self, generation: int, *, healthy: bool) -> None:
        if generation != self._generation:
            return
        if self._state is BreakerState.HALF_OPEN:
            if not healthy:
                self._transition(BreakerState.OPEN)
                return
            self._probes_succeeded += 1
            if self._probes_succeeded >= self._config.half_open_probes:
                self._transition(BreakerState.CLOSED)
            return
        self._outcomes.append(healthy)
        if len(self._outcomes) >= self._config.minimum_calls:
            failures = self._outcomes.count(False)
            if failures / len(self._outcomes) >= self._config.failure_rate:
                self._transition(BreakerState.OPEN)

    def _transition(self, new_state: BreakerState) -> None:
        previous = self._state
        self._state = new_state
        self._generation += 1
        self._outcomes.clear()
        self._probes_started = 0
        self._probes_succeeded = 0
        if new_state is BreakerState.OPEN:
            self._opened_at = self._clock()
        logger.warning(
            "Circuit breaker state changed",
            extra={
                "dependency": self.dependency,
                "from_state": previous.value,
                "to_state": new_state.value,
            },
        )
        if self._on_state_change is not None:
            self._on_state_change(self.dependency, previous, new_state)
