"""Run remote calls with a deadline, a circuit breaker and bounded retries (SRS section 7.7)."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from windops_resilience.breaker import CircuitBreaker
from windops_resilience.classify import is_dependency_failure


@dataclass(frozen=True)
class RetryPolicy:
    """Retry settings. max_attempts counts the first call, and the SRS allows at most 3."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.1
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("Retry delays must be non-negative and correctly ordered")


async def call_with_resilience[T](
    breaker: CircuitBreaker,
    operation: Callable[[float], Awaitable[T]],
    *,
    idempotent: bool,
    deadline_seconds: float,
    retry: RetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fraction: Callable[[], float] = random.random,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Call a dependency within one overall deadline.

    The operation receives the seconds remaining, to use as its own gRPC or HTTP deadline.
    Only idempotent calls are retried, and only after a dependency failure. A caller error
    or an open breaker is raised at once (RES-004). Disable a client SDK's own retries when
    using this function, so retry layers do not multiply (RES-003).
    """
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    policy = retry if retry is not None else RetryPolicy()
    expires = clock() + deadline_seconds
    attempts = policy.max_attempts if idempotent else 1

    async def attempt() -> T:
        remaining = expires - clock()
        async with asyncio.timeout(remaining):
            return await operation(remaining)

    for number in range(1, attempts + 1):
        try:
            return await breaker.call(attempt)
        except Exception as error:
            if number == attempts or not is_dependency_failure(error):
                raise
            backoff = min(policy.max_delay_seconds, policy.base_delay_seconds * 2 ** (number - 1))
            delay = random_fraction() * backoff
            if delay >= expires - clock():
                raise
            await sleep(delay)
    raise AssertionError("Unreachable: the final attempt returns or raises")
