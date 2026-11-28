"""A bulkhead keeps each dependency's calls in flight bounded (RES-002)."""

import asyncio
import logging

import grpc
import pytest
from conftest import FakeClock, FakeRpcError
from windops_resilience import (
    BreakerConfig,
    BreakerOpenError,
    BreakerState,
    Bulkhead,
    BulkheadConfig,
    BulkheadFullError,
    CircuitBreaker,
    call_with_resilience,
)


def breaker(
    bulkhead: BulkheadConfig, clock: FakeClock | None = None, **hooks: object
) -> CircuitBreaker:
    return CircuitBreaker(
        "weather",
        BreakerConfig(slow_call_seconds=5.0, minimum_calls=1),
        bulkhead=bulkhead,
        clock=clock or FakeClock(),
        **hooks,  # type: ignore[arg-type]
    )


class Held:
    """Calls that wait until released, standing in for a slow dependency."""

    def __init__(self) -> None:
        self.started = 0
        self._gate = asyncio.Event()

    async def call(self) -> str:
        self.started += 1
        await self._gate.wait()
        return "ok"

    def release(self) -> None:
        self._gate.set()


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_every_breaker_has_a_bulkhead_with_bounded_defaults() -> None:
    plain = CircuitBreaker("weather", BreakerConfig(slow_call_seconds=5.0))
    assert plain.bulkhead.in_flight == 0
    config = BulkheadConfig()
    # One dependency may hold at most 48 of the 128 gRPC workers.
    assert config.max_concurrent + config.max_waiting < 128


@pytest.mark.parametrize(
    "settings",
    [{"max_concurrent": 0}, {"max_waiting": -1}, {"max_wait_seconds": -0.1}],
)
def test_invalid_settings_are_refused(settings: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        BulkheadConfig(**settings)  # type: ignore[arg-type]


def test_a_full_bulkhead_refuses_without_calling_the_dependency() -> None:
    async def scenario() -> None:
        held = Held()
        guard = breaker(BulkheadConfig(max_concurrent=2, max_waiting=0))
        running = [asyncio.create_task(guard.call(held.call)) for _ in range(2)]
        await settle()
        with pytest.raises(BulkheadFullError) as refused:
            await guard.call(held.call)
        assert refused.value.dependency == "weather"
        assert held.started == 2
        held.release()
        assert await asyncio.gather(*running) == ["ok", "ok"]
        assert guard.bulkhead.in_flight == 0

    asyncio.run(scenario())


def test_a_refusal_is_a_breaker_open_error_so_fallbacks_apply() -> None:
    assert issubclass(BulkheadFullError, BreakerOpenError)


def test_a_refusal_does_not_count_against_the_dependency() -> None:
    async def scenario() -> None:
        held = Held()
        guard = breaker(BulkheadConfig(max_concurrent=1, max_waiting=0))
        running = asyncio.create_task(guard.call(held.call))
        await settle()
        for _ in range(20):
            with pytest.raises(BulkheadFullError):
                await guard.call(held.call)
        assert guard.state is BreakerState.CLOSED
        held.release()
        await running

    asyncio.run(scenario())


def test_a_waiter_gets_the_next_free_slot() -> None:
    async def scenario() -> None:
        held = Held()
        guard = breaker(BulkheadConfig(max_concurrent=1, max_waiting=1, max_wait_seconds=5))
        first = asyncio.create_task(guard.call(held.call))
        await settle()
        second = asyncio.create_task(guard.call(held.call))
        await settle()
        assert (held.started, guard.bulkhead.waiting) == (1, 1)
        held.release()
        assert list(await asyncio.gather(first, second)) == ["ok", "ok"]
        assert (guard.bulkhead.in_flight, guard.bulkhead.waiting) == (0, 0)

    asyncio.run(scenario())


def test_waiters_beyond_the_queue_are_refused_at_once() -> None:
    async def scenario() -> None:
        held = Held()
        guard = breaker(BulkheadConfig(max_concurrent=1, max_waiting=1, max_wait_seconds=5))
        tasks = [asyncio.create_task(guard.call(held.call)) for _ in range(2)]
        await settle()
        with pytest.raises(BulkheadFullError):
            await guard.call(held.call)
        held.release()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_a_waiter_gives_up_after_the_wait_limit() -> None:
    async def scenario() -> None:
        held = Held()
        guard = breaker(BulkheadConfig(max_concurrent=1, max_waiting=4, max_wait_seconds=0.01))
        running = asyncio.create_task(guard.call(held.call))
        await settle()
        with pytest.raises(BulkheadFullError):
            await guard.call(held.call)
        assert guard.bulkhead.waiting == 0
        held.release()
        await running
        assert guard.bulkhead.in_flight == 0

    asyncio.run(scenario())


def test_a_cancelled_waiter_leaves_no_slot_behind() -> None:
    async def scenario() -> None:
        held = Held()
        guard = breaker(BulkheadConfig(max_concurrent=1, max_waiting=4, max_wait_seconds=5))
        running = asyncio.create_task(guard.call(held.call))
        await settle()
        waiter = asyncio.create_task(guard.call(held.call))
        await settle()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        held.release()
        await running
        assert (guard.bulkhead.in_flight, guard.bulkhead.waiting) == (0, 0)
        assert await guard.call(held.call) == "ok"

    asyncio.run(scenario())


def test_a_slot_handed_to_a_waiter_as_it_is_cancelled_is_returned() -> None:
    async def scenario() -> None:
        bulkhead = Bulkhead("weather", BulkheadConfig(max_concurrent=1, max_wait_seconds=5))
        await bulkhead.acquire()
        waiter = asyncio.create_task(bulkhead.acquire())
        await settle()
        bulkhead.release()  # The slot passes to the waiter...
        waiter.cancel()  # ...which is cancelled before it runs again.
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (bulkhead.in_flight, bulkhead.waiting) == (0, 0)

    asyncio.run(scenario())


def test_slots_are_released_after_failures() -> None:
    async def fail() -> str:
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    async def scenario() -> None:
        guard = CircuitBreaker(
            "weather", BreakerConfig(slow_call_seconds=5.0), bulkhead=BulkheadConfig(1, 0)
        )
        for _ in range(3):
            with pytest.raises(FakeRpcError):
                await guard.call(fail)
        assert guard.bulkhead.in_flight == 0

    asyncio.run(scenario())


def test_a_refused_half_open_probe_is_not_used_up() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        guard = breaker(BulkheadConfig(max_concurrent=1, max_waiting=0), clock)

        async def fail() -> str:
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

        with pytest.raises(FakeRpcError):
            await guard.call(fail)
        clock.advance(30)
        assert guard.state.value == "half_open"
        held = Held()
        probe = asyncio.create_task(guard.call(held.call))
        await settle()
        with pytest.raises(BulkheadFullError):
            await guard.call(held.call)
        held.release()
        await probe

        async def succeed() -> str:
            return "ok"

        # Two probes remain, as the refused call took none of the three.
        await guard.call(succeed)
        await guard.call(succeed)
        assert guard.state is BreakerState.CLOSED

    asyncio.run(scenario())


def test_refusals_are_not_retried() -> None:
    async def scenario() -> None:
        held = Held()
        guard = breaker(BulkheadConfig(max_concurrent=1, max_waiting=0))
        running = asyncio.create_task(guard.call(held.call))
        await settle()
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        async def attempt(_: float) -> str:
            return await held.call()

        with pytest.raises(BulkheadFullError):
            await call_with_resilience(
                guard, attempt, idempotent=True, deadline_seconds=5, sleep=sleep
            )
        assert sleeps == []
        held.release()
        await running

    asyncio.run(scenario())


def test_refusals_call_the_hook_each_time_and_log_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refused: list[str] = []

    async def scenario() -> None:
        held = Held()
        guard = breaker(
            BulkheadConfig(max_concurrent=1, max_waiting=0), on_bulkhead_full=refused.append
        )
        running = asyncio.create_task(guard.call(held.call))
        await settle()
        for _ in range(3):
            with pytest.raises(BulkheadFullError):
                await guard.call(held.call)
        held.release()
        await running

    with caplog.at_level(logging.WARNING, logger="windops.resilience"):
        asyncio.run(scenario())
    assert refused == ["weather"] * 3
    full = [record for record in caplog.records if record.getMessage().startswith("Bulkhead")]
    assert len(full) == 1


def test_a_slow_dependency_leaves_others_untouched() -> None:
    """The RES-002 promise: a slow dependency fills its own bulkhead, not the service."""

    async def scenario() -> None:
        slow = Held()
        weather = breaker(BulkheadConfig(max_concurrent=4, max_waiting=0))
        policy = CircuitBreaker(
            "policy", BreakerConfig(slow_call_seconds=5.0), bulkhead=BulkheadConfig(4, 0)
        )
        stuck = [asyncio.create_task(weather.call(slow.call)) for _ in range(4)]
        await settle()

        async def quick() -> str:
            return "decided"

        assert [await policy.call(quick) for _ in range(20)] == ["decided"] * 20
        with pytest.raises(BulkheadFullError):
            await weather.call(slow.call)
        slow.release()
        await asyncio.gather(*stuck)

    asyncio.run(scenario())
