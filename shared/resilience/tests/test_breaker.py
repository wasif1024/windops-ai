import asyncio
import contextlib
import logging

import grpc
import pytest
from conftest import FakeClock, FakeRpcError
from windops_resilience import BreakerConfig, BreakerOpenError, BreakerState, CircuitBreaker


def make_breaker(clock: FakeClock, **overrides: float) -> CircuitBreaker:
    config = BreakerConfig(slow_call_seconds=5.0, **overrides)  # type: ignore[arg-type]
    return CircuitBreaker("telemetry", config, clock=clock)


async def succeed() -> str:
    return "ok"


async def fail() -> str:
    raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)


async def denied() -> str:
    raise FakeRpcError(grpc.StatusCode.PERMISSION_DENIED)


def run_calls(breaker: CircuitBreaker, *operations: object) -> None:
    async def runner() -> None:
        for operation in operations:
            with contextlib.suppress(FakeRpcError):
                await breaker.call(operation)  # type: ignore[arg-type]

    asyncio.run(runner())


def test_defaults_match_the_srs() -> None:
    config = BreakerConfig(slow_call_seconds=1.0)
    assert config.window_size == 20
    assert config.minimum_calls == 10
    assert config.failure_rate == 0.5
    assert config.open_seconds == 30.0
    assert config.half_open_probes == 3


def test_stays_closed_below_the_minimum_calls() -> None:
    breaker = make_breaker(FakeClock())
    run_calls(breaker, *[fail] * 9)
    assert breaker.state is BreakerState.CLOSED


def test_opens_at_half_failures_with_the_minimum_calls() -> None:
    breaker = make_breaker(FakeClock())
    run_calls(breaker, *[succeed] * 5, *[fail] * 5)
    assert breaker.state is BreakerState.OPEN


def test_stays_closed_just_below_the_threshold() -> None:
    breaker = make_breaker(FakeClock())
    run_calls(breaker, *[succeed] * 6, *[fail] * 4)
    assert breaker.state is BreakerState.CLOSED


def test_window_forgets_calls_older_than_twenty() -> None:
    breaker = make_breaker(FakeClock())
    run_calls(breaker, *[fail] * 4, *[succeed] * 6)  # 4 of 10 fail, so it stays closed.
    run_calls(breaker, *[succeed] * 10)
    run_calls(breaker, *[fail] * 9)
    assert breaker.state is BreakerState.CLOSED  # 9 of the last 20 fail.
    run_calls(breaker, fail)
    assert breaker.state is BreakerState.OPEN  # 10 of the last 20 fail.


def test_caller_errors_never_open_the_breaker() -> None:
    breaker = make_breaker(FakeClock())
    run_calls(breaker, *[denied] * 20)
    assert breaker.state is BreakerState.CLOSED


def test_slow_successes_count_as_failures() -> None:
    clock = FakeClock()
    breaker = make_breaker(clock)

    async def slow() -> str:
        clock.advance(5.0)
        return "late"

    run_calls(breaker, *[slow] * 10)
    assert breaker.state is BreakerState.OPEN


def open_breaker(clock: FakeClock) -> CircuitBreaker:
    breaker = make_breaker(clock)
    run_calls(breaker, *[fail] * 10)
    assert breaker.state is BreakerState.OPEN
    return breaker


def test_open_breaker_refuses_without_calling() -> None:
    breaker = open_breaker(FakeClock())
    calls = 0

    async def counted() -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(BreakerOpenError):
        asyncio.run(breaker.call(counted))
    assert calls == 0


def test_half_open_only_after_the_open_duration() -> None:
    clock = FakeClock()
    breaker = open_breaker(clock)
    clock.advance(29.9)
    assert breaker.state is BreakerState.OPEN
    clock.advance(0.1)
    assert breaker.state is BreakerState.HALF_OPEN


def test_three_successful_probes_close_the_breaker() -> None:
    clock = FakeClock()
    breaker = open_breaker(clock)
    clock.advance(30)
    run_calls(breaker, succeed, succeed)
    assert breaker.state is BreakerState.HALF_OPEN
    run_calls(breaker, succeed)
    assert breaker.state is BreakerState.CLOSED


@pytest.mark.parametrize("successes", [0, 1, 2])
def test_any_failed_probe_reopens_the_breaker(successes: int) -> None:
    clock = FakeClock()
    breaker = open_breaker(clock)
    clock.advance(30)
    run_calls(breaker, *[succeed] * successes, fail)
    assert breaker.state is BreakerState.OPEN


def test_reopened_breaker_waits_the_full_duration_again() -> None:
    clock = FakeClock()
    breaker = open_breaker(clock)
    clock.advance(30)
    run_calls(breaker, fail)
    clock.advance(29)
    assert breaker.state is BreakerState.OPEN


def test_closed_breaker_starts_with_an_empty_window() -> None:
    clock = FakeClock()
    breaker = open_breaker(clock)
    clock.advance(30)
    run_calls(breaker, succeed, succeed, succeed)
    run_calls(breaker, *[fail] * 9)
    assert breaker.state is BreakerState.CLOSED


def test_extra_concurrent_probes_are_refused() -> None:
    clock = FakeClock()
    breaker = open_breaker(clock)
    clock.advance(30)

    async def scenario() -> list[object]:
        release = asyncio.Event()

        async def probe() -> str:
            await release.wait()
            return "ok"

        tasks = [asyncio.create_task(breaker.call(probe)) for _ in range(4)]
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(scenario())
    assert results.count("ok") == 3
    assert sum(isinstance(result, BreakerOpenError) for result in results) == 1
    assert breaker.state is BreakerState.CLOSED


def test_late_result_from_before_a_state_change_is_ignored() -> None:
    clock = FakeClock()
    breaker = make_breaker(clock)

    async def scenario() -> None:
        release = asyncio.Event()

        async def slow_success() -> str:
            await release.wait()
            return "ok"

        straggler = asyncio.create_task(breaker.call(slow_success))
        await asyncio.sleep(0)
        for _ in range(10):
            with pytest.raises(FakeRpcError):
                await breaker.call(fail)
        assert breaker.state is BreakerState.OPEN
        clock.advance(30)
        assert breaker.state is BreakerState.HALF_OPEN
        release.set()
        await straggler
        # The straggler must not count as one of the three probes.
        await breaker.call(succeed)
        await breaker.call(succeed)
        assert breaker.state is BreakerState.HALF_OPEN

    asyncio.run(scenario())


def test_cancelled_probe_frees_its_slot() -> None:
    clock = FakeClock()
    breaker = open_breaker(clock)
    clock.advance(30)

    async def scenario() -> None:
        async def hang() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(breaker.call(hang))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(3):
            await breaker.call(succeed)

    asyncio.run(scenario())
    assert breaker.state is BreakerState.CLOSED


def test_state_changes_are_logged_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    changes: list[tuple[str, BreakerState, BreakerState]] = []
    clock = FakeClock()
    breaker = CircuitBreaker(
        "weather",
        BreakerConfig(slow_call_seconds=5.0),
        clock=clock,
        on_state_change=lambda name, old, new: changes.append((name, old, new)),
    )
    with caplog.at_level(logging.WARNING, logger="windops.resilience"):
        run_calls(breaker, *[fail] * 10)
    assert changes == [("weather", BreakerState.CLOSED, BreakerState.OPEN)]
    record = caplog.records[-1]
    assert record.__dict__["dependency"] == "weather"
    assert record.__dict__["to_state"] == "open"


@pytest.mark.parametrize(
    "overrides",
    [
        {"slow_call_seconds": 0},
        {"slow_call_seconds": 1, "open_seconds": 0},
        {"slow_call_seconds": 1, "minimum_calls": 21},
        {"slow_call_seconds": 1, "failure_rate": 1.5},
        {"slow_call_seconds": 1, "half_open_probes": 0},
    ],
)
def test_invalid_settings_are_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        BreakerConfig(**overrides)  # type: ignore[arg-type]
