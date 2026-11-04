"""Redis calls behind a circuit breaker, with each outage behavior from SRS 7.7."""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from windops_resilience import BreakerConfig, BreakerOpenError, CircuitBreaker

from windops_cache.keys import build_key, validate_ttl

logger = logging.getLogger(__name__)

# A cache that is slower than this is not saving the caller any time.
SLOW_CALL_SECONDS = 0.25
SOCKET_TIMEOUT_SECONDS = 0.5


class CacheUnavailableError(ConnectionError):
    """Redis cannot be reached. A ConnectionError, so it counts against the breaker."""


class RateLimitUnavailableError(Exception):
    """A rate limit cannot be checked, so a protected route must refuse the request."""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int


def cache_breaker() -> CircuitBreaker:
    """Return a calling service's breaker for Redis, with the SRS 7.7 defaults."""
    return CircuitBreaker("redis", BreakerConfig(slow_call_seconds=SLOW_CALL_SECONDS))


def connect(*, host: str, port: int, username: str, password: str) -> "redis.Redis":
    """Return a client that fails fast, leaving retries and outages to the breaker."""
    return redis.Redis(
        host=host,
        port=port,
        username=username,
        password=password,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        # The breaker decides what happens next, so retry layers cannot multiply (RES-003).
        retry=Retry(NoBackoff(), 0),
    )


async def _guarded[T](breaker: CircuitBreaker, operation: Callable[[], Awaitable[T]]) -> T:
    """Run a Redis call through the breaker, as an error the breaker counts as a failure."""

    async def translated() -> T:
        try:
            return await operation()
        except (RedisConnectionError, RedisTimeoutError, OSError) as error:
            # Redis's own errors are not ConnectionError, so the breaker would miss an outage.
            raise CacheUnavailableError(str(error)) from error

    return await breaker.call(translated)


class TenantCache:
    """A cache for one service and tenant. When Redis is down, it behaves like an empty cache."""

    def __init__(
        self, client: "redis.Redis", *, service: str, tenant_id: str, breaker: CircuitBreaker
    ) -> None:
        build_key(service, tenant_id, "check")
        self._client = client
        self._service = service
        self._tenant_id = tenant_id
        self._breaker = breaker

    def _key(self, name: str) -> str:
        return build_key(self._service, self._tenant_id, name)

    async def get(self, name: str) -> bytes | None:
        """Return a value, or None on a miss or when Redis is down."""
        key = self._key(name)
        try:
            value = await _guarded(self._breaker, lambda: self._client.get(key))
        except (CacheUnavailableError, BreakerOpenError):
            logger.warning("Cache read skipped, because Redis is unavailable")
            return None
        return value.encode() if isinstance(value, str) else value

    async def set(self, name: str, value: bytes, *, ttl_seconds: int) -> bool:
        """Store a value that expires, and return whether it was stored."""
        key = self._key(name)
        ttl = validate_ttl(ttl_seconds)
        try:
            await _guarded(self._breaker, lambda: self._client.set(key, value, ex=ttl))
        except (CacheUnavailableError, BreakerOpenError):
            logger.warning("Cache write skipped, because Redis is unavailable")
            return False
        return True

    async def delete(self, name: str) -> bool:
        """Remove a value, and return whether Redis could be reached."""
        key = self._key(name)
        try:
            await _guarded(self._breaker, lambda: self._client.delete(key))
        except (CacheUnavailableError, BreakerOpenError):
            logger.warning("Cache delete skipped, so the entry stays until its TTL ends")
            return False
        return True


class RateLimiter:
    """Fixed-window limits for one service and tenant. Fails closed when Redis is down."""

    def __init__(
        self,
        client: "redis.Redis",
        *,
        service: str,
        tenant_id: str,
        breaker: CircuitBreaker,
        clock: Callable[[], float] = time.time,
    ) -> None:
        build_key(service, tenant_id, "check")
        self._client = client
        self._service = service
        self._tenant_id = tenant_id
        self._breaker = breaker
        self._clock = clock

    async def allow(self, name: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        """Count a request. Raises RateLimitUnavailableError if the count cannot be made."""
        if limit < 1:
            raise ValueError("A rate limit must be at least 1")
        ttl = validate_ttl(window_seconds)
        window = int(self._clock() // ttl)
        key = build_key(self._service, self._tenant_id, f"rate:{name}:{window}")

        async def count() -> int:
            # In one transaction, so a crash cannot leave a counter that never expires.
            async with self._client.pipeline(transaction=True) as pipeline:
                pipeline.incr(key)
                pipeline.expire(key, ttl)
                results = await pipeline.execute()
            return int(results[0])

        try:
            used = await _guarded(self._breaker, count)
        except (CacheUnavailableError, BreakerOpenError) as error:
            raise RateLimitUnavailableError("The rate limit cannot be checked") from error
        return RateLimitDecision(allowed=used <= limit, remaining=max(limit - used, 0))
