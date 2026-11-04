"""The cache's behavior when Redis works, and each SRS 7.7 behavior when it does not."""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, Self, cast

import pytest
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from windops_cache import (
    RateLimiter,
    RateLimitUnavailableError,
    TenantCache,
    cache_breaker,
    connect,
)
from windops_resilience import BreakerState, CircuitBreaker


def run_async[**P](test: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Run an async test, because the repository uses no async test plugin."""

    @functools.wraps(test)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


class FakePipeline:
    def __init__(self, owner: "FakeRedis") -> None:
        self._owner = owner
        self._key = ""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def incr(self, key: str) -> None:
        self._key = key

    def expire(self, key: str, ttl: int) -> None:
        self._owner.ttls[key] = ttl

    async def execute(self) -> list[int]:
        self._owner.raise_if_down()
        count = int(self._owner.values.get(self._key, b"0")) + 1
        self._owner.values[self._key] = str(count).encode()
        return [count, 1]


class FakeRedis:
    """Implements only the calls the library makes, with a switchable outage."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.error: Exception | None = None
        self.calls = 0

    def raise_if_down(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error

    async def get(self, key: str) -> bytes | None:
        self.raise_if_down()
        return self.values.get(key)

    async def set(self, key: str, value: bytes, ex: int) -> None:
        self.raise_if_down()
        self.values[key] = value
        self.ttls[key] = ex

    async def delete(self, key: str) -> None:
        self.raise_if_down()
        self.values.pop(key, None)

    def pipeline(self, transaction: bool) -> FakePipeline:
        assert transaction
        return FakePipeline(self)


@pytest.fixture
def fake() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def breaker() -> CircuitBreaker:
    return cache_breaker()


def client(fake: FakeRedis) -> "redis.Redis":
    return cast("redis.Redis", cast(Any, fake))


def cache_for(fake: FakeRedis, breaker: CircuitBreaker, tenant: str = "tenant-a") -> TenantCache:
    return TenantCache(client(fake), service="weather", tenant_id=tenant, breaker=breaker)


def limiter_for(fake: FakeRedis, breaker: CircuitBreaker) -> RateLimiter:
    return RateLimiter(
        client(fake), service="weather", tenant_id="tenant-a", breaker=breaker, clock=lambda: 1000.0
    )


@run_async
async def test_value_is_stored_with_its_ttl(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    cache = cache_for(fake, breaker)
    assert await cache.set("forecast", b"sunny", ttl_seconds=60)
    assert await cache.get("forecast") == b"sunny"
    assert fake.ttls["weather:tenant-a:forecast"] == 60


@run_async
async def test_one_tenant_cannot_read_anothers_entry(
    fake: FakeRedis, breaker: CircuitBreaker
) -> None:
    """DAT-007."""
    await cache_for(fake, breaker, "tenant-a").set("forecast", b"a", ttl_seconds=60)
    assert await cache_for(fake, breaker, "tenant-b").get("forecast") is None


@run_async
async def test_miss_returns_none(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    assert await cache_for(fake, breaker).get("forecast") is None


@run_async
async def test_delete_removes_the_entry(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    cache = cache_for(fake, breaker)
    await cache.set("forecast", b"a", ttl_seconds=60)
    assert await cache.delete("forecast")
    assert await cache.get("forecast") is None


@run_async
async def test_set_without_a_valid_ttl_is_refused_before_any_call(
    fake: FakeRedis, breaker: CircuitBreaker
) -> None:
    with pytest.raises(ValueError, match="TTL"):
        await cache_for(fake, breaker).set("forecast", b"a", ttl_seconds=0)
    assert fake.calls == 0


@run_async
async def test_credentials_are_refused_before_any_call(
    fake: FakeRedis, breaker: CircuitBreaker
) -> None:
    with pytest.raises(ValueError, match="DAT-004"):
        await cache_for(fake, breaker).set("access-token", b"a", ttl_seconds=60)
    assert fake.calls == 0


def test_invalid_tenant_is_refused_when_created(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    with pytest.raises(ValueError, match="tenant"):
        cache_for(fake, breaker, "a:b")


@pytest.mark.parametrize(
    "error", [RedisConnectionError("down"), RedisTimeoutError("slow"), OSError("reset")]
)
@run_async
async def test_cache_acts_empty_when_redis_is_down(
    fake: FakeRedis, breaker: CircuitBreaker, error: Exception
) -> None:
    """SRS 7.7: work continues from the source service."""
    cache = cache_for(fake, breaker)
    fake.error = error
    assert await cache.get("forecast") is None
    assert await cache.set("forecast", b"a", ttl_seconds=60) is False
    assert await cache.delete("forecast") is False


@run_async
async def test_redis_errors_open_the_breaker(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    """Redis's own errors must count, or an outage would never open the breaker."""
    cache = cache_for(fake, breaker)
    fake.error = RedisConnectionError("down")
    for _ in range(10):
        await cache.get("forecast")
    assert breaker.state is BreakerState.OPEN
    calls = fake.calls
    assert await cache.get("forecast") is None
    assert fake.calls == calls


@run_async
async def test_a_redis_permission_error_is_raised_and_does_not_open_the_breaker(
    fake: FakeRedis, breaker: CircuitBreaker
) -> None:
    """A wrong key or login is a bug to surface, and says nothing about Redis's health."""
    cache = cache_for(fake, breaker)
    fake.error = ResponseError("NOPERM")
    for _ in range(10):
        with pytest.raises(ResponseError):
            await cache.get("forecast")
    assert breaker.state is BreakerState.CLOSED


@run_async
async def test_rate_limit_allows_up_to_the_limit(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    limiter = limiter_for(fake, breaker)
    decisions = [await limiter.allow("login", limit=2, window_seconds=60) for _ in range(3)]
    assert [item.allowed for item in decisions] == [True, True, False]
    assert [item.remaining for item in decisions] == [1, 0, 0]


@run_async
async def test_rate_limit_counter_expires(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    await limiter_for(fake, breaker).allow("login", limit=2, window_seconds=60)
    assert list(fake.ttls.values()) == [60]
    assert next(iter(fake.ttls)).startswith("weather:tenant-a:rate:login:")


@run_async
async def test_a_new_window_starts_a_new_count(fake: FakeRedis, breaker: CircuitBreaker) -> None:
    times = iter([1000.0, 1000.0, 1061.0])
    limiter = RateLimiter(
        client(fake),
        service="weather",
        tenant_id="tenant-a",
        breaker=breaker,
        clock=lambda: next(times),
    )
    assert (await limiter.allow("login", limit=1, window_seconds=60)).allowed
    assert not (await limiter.allow("login", limit=1, window_seconds=60)).allowed
    assert (await limiter.allow("login", limit=1, window_seconds=60)).allowed


@run_async
async def test_rate_limit_fails_closed_when_redis_is_down(
    fake: FakeRedis, breaker: CircuitBreaker
) -> None:
    """SRS 7.7: a limit that cannot be checked must not let the request through."""
    fake.error = RedisConnectionError("down")
    with pytest.raises(RateLimitUnavailableError):
        await limiter_for(fake, breaker).allow("login", limit=2, window_seconds=60)


@run_async
async def test_rate_limit_fails_closed_when_the_breaker_is_open(
    fake: FakeRedis, breaker: CircuitBreaker
) -> None:
    limiter = limiter_for(fake, breaker)
    fake.error = RedisConnectionError("down")
    for _ in range(10):
        with pytest.raises(RateLimitUnavailableError):
            await limiter.allow("login", limit=2, window_seconds=60)
    assert breaker.state is BreakerState.OPEN
    fake.error = None
    with pytest.raises(RateLimitUnavailableError):
        await limiter.allow("login", limit=2, window_seconds=60)


@pytest.mark.parametrize("limit", [0, -1])
@run_async
async def test_a_limit_below_one_is_refused(
    fake: FakeRedis, breaker: CircuitBreaker, limit: int
) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await limiter_for(fake, breaker).allow("login", limit=limit, window_seconds=60)


def test_the_client_has_no_retries_of_its_own() -> None:
    """RES-003: the client's retries must not multiply with any other layer."""
    created = connect(host="127.0.0.1", port=6379, username="user", password="value")
    retry = created.connection_pool.connection_kwargs["retry"]
    assert retry.get_retries() == 0
