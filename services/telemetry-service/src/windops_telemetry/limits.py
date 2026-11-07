"""Each device's rate limit from SRS 7.5, 600 requests a minute, counted in Redis. Fails closed."""

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

WINDOW_SECONDS = 60
DEVICE_LIMIT = 600
# Under this service's own prefix, which is all its Redis login may touch.
PREFIX = "telemetry"


class LimitUnavailableError(Exception):
    """The counters cannot be reached, so the request is refused rather than let through."""


class Counter(Protocol):
    def incrby(self, name: str, amount: int) -> Awaitable[Any]: ...
    def expire(self, name: str, time: int) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    reset_seconds: int


class DeviceLimiter:
    """A fixed one-minute window per device."""

    def __init__(
        self, redis: Counter, *, limit: int = DEVICE_LIMIT, clock: Callable[[], float] = time.time
    ) -> None:
        self._redis, self._limit, self._clock = redis, limit, clock

    async def check(self, tenant_id: str, device_id: str) -> Decision:
        now = self._clock()
        key = f"{PREFIX}:rate:{tenant_id}:{device_id}:{int(now // WINDOW_SECONDS)}"
        try:
            # INCRBY, as the service's Redis login allows it and not INCR.
            count = int(await self._redis.incrby(key, 1))
            if count == 1:
                await self._redis.expire(key, WINDOW_SECONDS * 2)
        except Exception as error:
            raise LimitUnavailableError(type(error).__name__) from error
        # Rounded up, so the last moment of a window never says retry after 0 seconds.
        reset = max(1, math.ceil(WINDOW_SECONDS - now % WINDOW_SECONDS))
        return Decision(count <= self._limit, max(self._limit - count, 0), reset)
