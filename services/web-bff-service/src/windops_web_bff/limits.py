"""Per-user rate limits from SRS 7.5, counted in Redis. They fail closed (SRS 7.7)."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

WINDOW_SECONDS = 60
# The administrators' client asks for a second factor at every sign-in, so its users get more.
ADMIN_CLIENT = "windops-admin"
LIMITS = {"standard": 120, "administrator": 300}
PREFIX = "web-bff"


class LimitUnavailableError(Exception):
    """The counters cannot be reached, so the request is refused rather than let through."""


class Counter(Protocol):
    def incrby(self, name: str, amount: int) -> Awaitable[Any]: ...
    def expire(self, name: str, time: int) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


def traffic_class(client: str) -> str:
    return "administrator" if client == ADMIN_CLIENT else "standard"


class RateLimiter:
    """A fixed one-minute window per user. Simple, and enough at this scale."""

    def __init__(self, redis: Counter, *, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._clock = clock

    async def check(self, tenant_id: str, user_id: str, client: str) -> Decision:
        kind = traffic_class(client)
        limit = LIMITS[kind]
        now = self._clock()
        window = int(now // WINDOW_SECONDS)
        # Under the service's own prefix, which is all its Redis login may touch.
        key = f"{PREFIX}:rate:{kind}:{tenant_id}:{user_id}:{window}"
        try:
            # INCRBY, because the service's Redis login allows it and not INCR.
            count = int(await self._redis.incrby(key, 1))
            if count == 1:
                await self._redis.expire(key, WINDOW_SECONDS * 2)
        except Exception as error:
            raise LimitUnavailableError(type(error).__name__) from error
        reset = int(WINDOW_SECONDS - now % WINDOW_SECONDS)
        return Decision(count <= limit, limit, max(limit - count, 0), reset)
