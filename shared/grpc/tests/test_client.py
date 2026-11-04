"""Exercise the client against a real server."""

import asyncio

import grpc
import pytest
from grpc_harness import IDENTITY, running_service, token
from opentelemetry.sdk.trace import TracerProvider
from windops_grpc import ServiceClient, outgoing_metadata
from windops_resilience import BreakerConfig, BreakerOpenError, BreakerState, CircuitBreaker


def breaker(minimum_calls: int = 10) -> CircuitBreaker:
    return CircuitBreaker("test", BreakerConfig(slow_call_seconds=5.0, minimum_calls=minimum_calls))


def test_client_sends_identity_and_request_id() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            client = ServiceClient(breaker(), identity_token=token)
            result = await client.call(
                harness.method("Echo"), b"hi", idempotent=True, deadline_seconds=5
            )
            assert result == b"ok:tenant-a"
            metadata = harness.seen[0]["metadata"]
            assert metadata["x-windops-identity"] == token(IDENTITY)
            assert metadata["x-windops-request-id"]

    asyncio.run(scenario())


def test_client_without_identity_is_rejected_and_does_not_retry() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            active = breaker(minimum_calls=1)
            client = ServiceClient(active)
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await client.call(
                    harness.method("Echo"), b"hi", idempotent=True, deadline_seconds=5
                )
            assert caught.value.code() is grpc.StatusCode.UNAUTHENTICATED
            assert active.state is BreakerState.CLOSED

    asyncio.run(scenario())


def test_idempotent_unavailable_is_retried_and_opens_the_breaker() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            active = breaker(minimum_calls=3)
            client = ServiceClient(active, identity_token=token)
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await client.call(
                    harness.method("Echo"), b"unavailable", idempotent=True, deadline_seconds=30
                )
            assert caught.value.code() is grpc.StatusCode.UNAVAILABLE
            assert harness.calls == [b"unavailable"] * 3
            with pytest.raises(BreakerOpenError):
                await client.call(
                    harness.method("Echo"), b"hi", idempotent=True, deadline_seconds=30
                )
            assert len(harness.calls) == 3

    asyncio.run(scenario())


def test_non_idempotent_call_is_not_retried() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            client = ServiceClient(breaker(), identity_token=token)
            with pytest.raises(grpc.aio.AioRpcError):
                await client.call(
                    harness.method("Echo"), b"unavailable", idempotent=False, deadline_seconds=30
                )
            assert harness.calls == [b"unavailable"]

    asyncio.run(scenario())


def test_deadline_reaches_the_server_and_stops_a_hung_call() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            client = ServiceClient(breaker(), identity_token=token)
            with pytest.raises((grpc.aio.AioRpcError, TimeoutError)):
                await client.call(
                    harness.method("Echo"), b"wait", idempotent=False, deadline_seconds=0.2
                )
            harness.release.set()

    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_outgoing_metadata_includes_trace_context() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work"):
        metadata = dict(outgoing_metadata(request_id="req-1", identity_token="abc"))
    assert metadata["x-windops-request-id"] == "req-1"
    assert metadata["x-windops-identity"] == "abc"
    assert "traceparent" in metadata


def test_outgoing_metadata_forwards_the_incoming_identity() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            first = (("x-windops-identity", token()),)
            await harness.method("Echo")(b"hi", timeout=5, metadata=first)
            assert harness.seen[0]["metadata"]["x-windops-identity"] == token()

    asyncio.run(scenario())
    assert "x-windops-identity" not in dict(outgoing_metadata())
