import asyncio
import time

import grpc
import pytest
from conftest import FakeClock, FakeRpcError
from windops_resilience import (
    BreakerConfig,
    BreakerOpenError,
    BreakerState,
    CircuitBreaker,
    RetryPolicy,
    call_with_resilience,
)


class Dependency:
    """Fails with the given errors in order, then succeeds."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.deadlines: list[float] = []

    async def __call__(self, remaining: float) -> str:
        self.calls += 1
        self.deadlines.append(remaining)
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


UNAVAILABLE = FakeRpcError(grpc.StatusCode.UNAVAILABLE)


def run(
    dependency: Dependency,
    *,
    idempotent: bool = True,
    breaker: CircuitBreaker | None = None,
    clock: FakeClock | None = None,
    deadline: float = 60.0,
    delays: list[float] | None = None,
    fraction: float = 1.0,
) -> str:
    fake_clock = clock if clock is not None else FakeClock()
    active = breaker or CircuitBreaker(
        "telemetry", BreakerConfig(slow_call_seconds=5.0), clock=fake_clock
    )

    async def fake_sleep(seconds: float) -> None:
        if delays is not None:
            delays.append(seconds)
        fake_clock.advance(seconds)

    return asyncio.run(
        call_with_resilience(
            active,
            dependency,
            idempotent=idempotent,
            deadline_seconds=deadline,
            sleep=fake_sleep,
            random_fraction=lambda: fraction,
            clock=fake_clock,
        )
    )


def test_success_needs_one_call() -> None:
    dependency = Dependency()
    assert run(dependency) == "ok"
    assert dependency.calls == 1


def test_idempotent_call_recovers_on_a_retry() -> None:
    dependency = Dependency(UNAVAILABLE, UNAVAILABLE)
    assert run(dependency) == "ok"
    assert dependency.calls == 3


def test_at_most_three_attempts_in_total() -> None:
    dependency = Dependency(UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE)
    with pytest.raises(FakeRpcError):
        run(dependency)
    assert dependency.calls == 3


def test_policy_cannot_exceed_three_attempts() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=4)


def test_non_idempotent_call_is_never_retried() -> None:
    dependency = Dependency(UNAVAILABLE)
    with pytest.raises(FakeRpcError):
        run(dependency, idempotent=False)
    assert dependency.calls == 1


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.NOT_FOUND,
    ],
)
def test_caller_errors_are_never_retried(code: grpc.StatusCode) -> None:
    dependency = Dependency(FakeRpcError(code))
    with pytest.raises(FakeRpcError):
        run(dependency)
    assert dependency.calls == 1


def test_backoff_doubles_and_applies_jitter() -> None:
    delays: list[float] = []
    run(Dependency(UNAVAILABLE, UNAVAILABLE), delays=delays, fraction=1.0)
    assert delays == [0.1, 0.2]
    delays.clear()
    run(Dependency(UNAVAILABLE, UNAVAILABLE), delays=delays, fraction=0.5)
    assert delays == [0.05, 0.1]


def test_each_attempt_counts_in_the_breaker() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("telemetry", BreakerConfig(slow_call_seconds=5.0), clock=clock)
    for _ in range(3):
        with pytest.raises(FakeRpcError):
            run(Dependency(*[UNAVAILABLE] * 3), breaker=breaker, clock=clock)
    with pytest.raises((FakeRpcError, BreakerOpenError)):
        run(Dependency(UNAVAILABLE), breaker=breaker, clock=clock)
    assert breaker.state is BreakerState.OPEN


def test_open_breaker_is_not_retried() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        "telemetry", BreakerConfig(slow_call_seconds=5.0, minimum_calls=1), clock=clock
    )
    first = Dependency(UNAVAILABLE)
    with pytest.raises(FakeRpcError):
        run(first, breaker=breaker, clock=clock, idempotent=False)
    second = Dependency()
    with pytest.raises(BreakerOpenError):
        run(second, breaker=breaker, clock=clock)
    assert second.calls == 0


def test_operation_receives_the_remaining_deadline() -> None:
    dependency = Dependency(UNAVAILABLE)
    run(dependency, deadline=10.0, fraction=1.0)
    assert dependency.deadlines == pytest.approx([10.0, 9.9])


def test_no_retry_when_the_backoff_would_pass_the_deadline() -> None:
    dependency = Dependency(UNAVAILABLE)
    with pytest.raises(FakeRpcError):
        run(dependency, deadline=0.05)
    assert dependency.calls == 1


def test_a_hung_call_stops_at_the_deadline_and_counts_as_a_failure() -> None:
    breaker = CircuitBreaker("telemetry", BreakerConfig(slow_call_seconds=5.0, minimum_calls=1))

    async def hang(remaining: float) -> str:
        await asyncio.Event().wait()
        return "never"

    async def scenario() -> None:
        # The outer limit makes a broken deadline fail this test instead of hanging it.
        call = call_with_resilience(breaker, hang, idempotent=False, deadline_seconds=0.01)
        async with asyncio.timeout(5):
            await call

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(scenario())
    assert time.monotonic() - started < 1
    assert breaker.state is BreakerState.OPEN


def test_invalid_deadline_is_rejected() -> None:
    with pytest.raises(ValueError):
        run(Dependency(), deadline=0)
