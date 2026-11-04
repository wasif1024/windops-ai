"""Run the library against real gRPC errors from a local in-process server."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import pytest
from windops_resilience import (
    BreakerConfig,
    BreakerOpenError,
    BreakerState,
    CircuitBreaker,
    call_with_resilience,
    is_dependency_failure,
)

METHOD = "/windops.test.v1.TestService/Call"


@asynccontextmanager
async def server_stub(
    code: grpc.StatusCode | None, *, delay: float = 0
) -> AsyncIterator[tuple[grpc.aio.UnaryUnaryMultiCallable[bytes, bytes], list[int]]]:
    calls: list[int] = []

    async def handler(request: bytes, context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
        calls.append(1)
        if delay:
            await asyncio.sleep(delay)
        if code is not None:
            await context.abort(code, "test")
        return b"ok"

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "windops.test.v1.TestService",
                {"Call": grpc.unary_unary_rpc_method_handler(handler)},
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield channel.unary_unary(METHOD), calls
    finally:
        await channel.close()
        await server.stop(None)


def new_breaker(minimum_calls: int = 10) -> CircuitBreaker:
    return CircuitBreaker("test", BreakerConfig(slow_call_seconds=5.0, minimum_calls=minimum_calls))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (grpc.StatusCode.UNAVAILABLE, True),
        (grpc.StatusCode.INTERNAL, True),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, True),
        (grpc.StatusCode.PERMISSION_DENIED, False),
        (grpc.StatusCode.UNAUTHENTICATED, False),
        (grpc.StatusCode.INVALID_ARGUMENT, False),
        (grpc.StatusCode.NOT_FOUND, False),
    ],
)
def test_real_status_codes_are_classified(code: grpc.StatusCode, expected: bool) -> None:
    async def scenario() -> None:
        async with server_stub(code) as (stub, _):
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await stub(b"x", timeout=5)
            assert is_dependency_failure(caught.value) is expected

    asyncio.run(scenario())


def test_real_deadline_exceeded_is_a_failure() -> None:
    async def scenario() -> None:
        async with server_stub(None, delay=5) as (stub, _):
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await stub(b"x", timeout=0.05)
            assert caught.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED
            assert is_dependency_failure(caught.value)

    asyncio.run(scenario())


def test_real_call_succeeds_with_the_remaining_deadline() -> None:
    async def scenario() -> None:
        async with server_stub(None) as (stub, calls):
            result = await call_with_resilience(
                new_breaker(),
                lambda remaining: stub(b"x", timeout=remaining),
                idempotent=True,
                deadline_seconds=5.0,
            )
            assert result == b"ok"
            assert len(calls) == 1

    asyncio.run(scenario())


def test_real_outage_retries_then_opens_the_breaker() -> None:
    async def scenario() -> None:
        breaker = new_breaker(minimum_calls=3)

        async def attempt() -> bytes:
            return await call_with_resilience(
                breaker,
                lambda remaining: stub(b"x", timeout=remaining),
                idempotent=True,
                deadline_seconds=30.0,
            )

        async with server_stub(grpc.StatusCode.UNAVAILABLE) as (stub, calls):
            with pytest.raises(grpc.aio.AioRpcError):
                await attempt()
            assert len(calls) == 3
            assert breaker.state is BreakerState.OPEN
            with pytest.raises(BreakerOpenError):
                await attempt()
            assert len(calls) == 3

    asyncio.run(scenario())


def test_real_permission_denied_is_not_retried_and_leaves_the_breaker_closed() -> None:
    async def scenario() -> None:
        breaker = new_breaker(minimum_calls=1)
        async with server_stub(grpc.StatusCode.PERMISSION_DENIED) as (stub, calls):
            with pytest.raises(grpc.aio.AioRpcError):
                await call_with_resilience(
                    breaker,
                    lambda remaining: stub(b"x", timeout=remaining),
                    idempotent=True,
                    deadline_seconds=5.0,
                )
            assert len(calls) == 1
            assert breaker.state is BreakerState.CLOSED

    asyncio.run(scenario())
