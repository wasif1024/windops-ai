"""How often a tool may be called: per caller each minute, per tenant each day (MCP-003).

Counts live in Redis, so every replica of a server shares them, under the server's own key prefix
and the caller's tenant. If they cannot be counted, the call is refused: limits fail closed.
"""

import hashlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from windops_cache import RateLimiter, RateLimitUnavailableError
from windops_resilience import CircuitBreaker

from windops_mcp.tokens import ToolCaller
from windops_mcp.tools import Tool

if TYPE_CHECKING:
    import redis.asyncio as redis

MINUTE = 60
DAY = 24 * 60 * 60


class LimitReachedError(Exception):
    """The caller's rate, or the tenant's daily quota, is used up."""

    def __init__(self, which: str) -> None:
        super().__init__(which)
        self.which = which


class LimitsUnavailableError(Exception):
    """The counts cannot be made, so nothing is let through."""


class Limits(Protocol):
    async def admit(self, caller: ToolCaller, tool: Tool) -> None:
        """Count one call, or raise LimitReachedError or LimitsUnavailableError."""


def _who(caller: ToolCaller) -> str:
    # A user's ID is not put in a key as it is, only a short hash of it.
    return hashlib.sha256(caller.user_id.encode()).hexdigest()[:16]


class RedisLimits:
    def __init__(self, client: "redis.Redis", *, service: str, breaker: CircuitBreaker) -> None:
        self._client = client
        self._service = service
        self._breaker = breaker

    async def admit(self, caller: ToolCaller, tool: Tool) -> None:
        limiter = RateLimiter(
            self._client, service=self._service, tenant_id=caller.tenant_id, breaker=self._breaker
        )
        try:
            rate = await limiter.allow(
                f"tool:{tool.name}:caller:{_who(caller)}", limit=tool.per_minute,
                window_seconds=MINUTE,
            )  # fmt: skip
            if not rate.allowed:
                raise LimitReachedError("per_minute")
            quota = await limiter.allow(f"tool:{tool.name}:day", limit=tool.per_day,
                                        window_seconds=DAY)  # fmt: skip
        except RateLimitUnavailableError as error:
            raise LimitsUnavailableError("The limits cannot be counted") from error
        if not quota.allowed:
            raise LimitReachedError("per_day")


class MemoryLimits:
    """The same counts in one process, for tests and a server run by hand."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._counts: dict[tuple[str, ...], int] = {}

    def _count(self, key: tuple[str, ...], window: int) -> int:
        bucket = (*key, str(int(self._clock() // window)))
        self._counts[bucket] = self._counts.get(bucket, 0) + 1
        return self._counts[bucket]

    async def admit(self, caller: ToolCaller, tool: Tool) -> None:
        if self._count((caller.tenant_id, tool.name, _who(caller)), MINUTE) > tool.per_minute:
            raise LimitReachedError("per_minute")
        if self._count((caller.tenant_id, tool.name), DAY) > tool.per_day:
            raise LimitReachedError("per_day")
