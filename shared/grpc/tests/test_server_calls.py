"""Exercise the interceptor through real gRPC calls."""

import asyncio
import json
import logging

import grpc
import pytest
from grpc_harness import DOMAIN_ERRORS, IDENTITY, SECRET, running_service, token
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from windops_grpc import ServiceClient, VerifiedIdentity, sign_identity
from windops_resilience import BreakerConfig, CircuitBreaker

GOOD = (("x-windops-identity", token()),)
EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


def test_valid_call_reaches_the_handler_with_verified_identity() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            result = await harness.method("Echo")(b"hi", timeout=5, metadata=GOOD)
            assert result == b"ok:tenant-a"
            assert harness.seen[0]["tenant"] == "tenant-a"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metadata",
    [
        (),
        (("x-windops-identity", "garbage"),),
        (("x-windops-identity", sign_identity(IDENTITY, "wrong-secret-that-is-long-enough-here")),),
        (("x-windops-identity", sign_identity(IDENTITY, SECRET, ttl_seconds=-60)),),
    ],
    ids=["missing", "garbage", "wrong-key", "expired"],
)
def test_unverified_call_is_rejected_before_the_handler(
    metadata: tuple[tuple[str, str], ...],
) -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await harness.method("Echo")(b"hi", timeout=5, metadata=metadata)
            assert caught.value.code() is grpc.StatusCode.UNAUTHENTICATED
            assert harness.calls == []

    asyncio.run(scenario())


def test_call_without_a_deadline_is_rejected() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await harness.method("Echo")(b"hi", metadata=GOOD)
            assert caught.value.code() is grpc.StatusCode.INVALID_ARGUMENT
            assert harness.calls == []

    asyncio.run(scenario())


def test_tenant_comes_from_the_signed_identity_and_not_other_metadata() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            metadata = (*GOOD, ("tenant_id", "tenant-b"), ("x-tenant-id", "tenant-b"))
            assert await harness.method("Echo")(b"hi", timeout=5, metadata=metadata) == (
                b"ok:tenant-a"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("request_bytes", sorted(DOMAIN_ERRORS))
def test_domain_errors_map_to_status_and_safe_message(request_bytes: bytes) -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await harness.method("Echo")(request_bytes, timeout=5, metadata=GOOD)
            error = DOMAIN_ERRORS[request_bytes]
            assert caught.value.code() is error.code  # type: ignore[attr-defined]
            assert caught.value.details() == str(error)

    asyncio.run(scenario())


def test_unexpected_error_hides_details_from_the_caller() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await harness.method("Echo")(b"crash", timeout=5, metadata=GOOD)
            assert caught.value.code() is grpc.StatusCode.INTERNAL
            assert caught.value.details() == "Internal error"

    asyncio.run(scenario())


def test_unexpected_error_is_logged_with_context(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            logging.getLogger().addHandler(caplog.handler)
            with pytest.raises(grpc.aio.AioRpcError):
                await harness.method("Echo")(b"crash", timeout=5, metadata=GOOD)

    asyncio.run(scenario())
    records = [r for r in caplog.records if r.message == "Call raised an unexpected error"]
    assert records
    assert records[0].exc_info is not None
    assert records[0].__dict__["rpc_method"].endswith("/Echo")


def test_streaming_call_works_and_checks_identity() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            call = harness.stream("Numbers")(b"go", timeout=5, metadata=GOOD)
            assert [item async for item in call] == [b"tenant-a", b"second"]
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                async for _ in harness.stream("Numbers")(b"go", timeout=5):
                    pass
            assert caught.value.code() is grpc.StatusCode.UNAUTHENTICATED
            assert harness.calls == [b"go"]

    asyncio.run(scenario())


def test_streaming_error_is_mapped_after_partial_results() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            received = []
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                call = harness.stream("Numbers")(b"missing", timeout=5, metadata=GOOD)
                async for item in call:
                    received.append(item)
            assert received == [b"tenant-a"]
            assert caught.value.code() is grpc.StatusCode.NOT_FOUND

    asyncio.run(scenario())


def test_trace_context_continues_from_the_caller() -> None:
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    metadata = (*GOOD, ("traceparent", f"00-{trace_id}-b7ad6b7169203331-01"))
    EXPORTER.clear()

    async def scenario() -> None:
        async with running_service() as harness:
            await harness.method("Echo")(b"hi", timeout=5, metadata=metadata)

    asyncio.run(scenario())
    span = next(item for item in EXPORTER.get_finished_spans() if item.name.endswith("/Echo"))
    assert span.context is not None
    assert format(span.context.trace_id, "032x") == trace_id
    assert span.attributes is not None
    assert span.attributes["windops.tenant_id"] == "tenant-a"


def test_rejected_call_is_still_traced_as_an_error() -> None:
    EXPORTER.clear()

    async def scenario() -> None:
        async with running_service() as harness:
            with pytest.raises(grpc.aio.AioRpcError):
                await harness.method("Echo")(b"hi", timeout=5)

    asyncio.run(scenario())
    span = next(item for item in EXPORTER.get_finished_spans() if item.name.endswith("/Echo"))
    assert span.status.status_code is trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes["rpc.grpc.status"] == "UNAUTHENTICATED"


def test_successful_call_records_its_status_on_the_span() -> None:
    EXPORTER.clear()

    async def scenario() -> None:
        async with running_service() as harness:
            await harness.method("Echo")(b"hi", timeout=5, metadata=GOOD)

    asyncio.run(scenario())
    span = next(item for item in EXPORTER.get_finished_spans() if item.name.endswith("/Echo"))
    assert span.attributes is not None
    assert span.attributes["rpc.grpc.status"] == "OK"


def test_a_service_principal_needs_no_user() -> None:
    principal = VerifiedIdentity(subject="fault-investigator", tenant_id="tenant-a")
    metadata = (("x-windops-identity", token(principal)),)

    async def scenario() -> None:
        async with running_service() as harness:
            assert await harness.method("Echo")(b"hi", timeout=5, metadata=metadata) == (
                b"ok:tenant-a"
            )

    asyncio.run(scenario())


def call_log_entries(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    lines = capsys.readouterr().out.splitlines()
    entries = [json.loads(line) for line in lines if line.startswith("{")]
    return [entry for entry in entries if entry["logger"] == "windops.grpc"]


def test_finished_call_is_logged_with_trace_request_and_tenant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    trace_id = "1af7651916cd43dd8448eb211c80319c"
    metadata = (
        *GOOD,
        ("traceparent", f"00-{trace_id}-b7ad6b7169203331-01"),
        ("x-windops-request-id", "req-42"),
    )

    async def scenario() -> None:
        async with running_service() as harness:
            await harness.method("Echo")(b"hi", timeout=5, metadata=metadata)

    asyncio.run(scenario())
    entry = next(item for item in call_log_entries(capsys) if item["message"] == "Call finished")
    assert entry["trace_id"] == trace_id
    assert entry["request_id"] == "req-42"
    assert entry["tenant_id"] == "tenant-a"
    assert entry["rpc_status"] == "OK"


def test_rejected_call_log_keeps_its_trace(capsys: pytest.CaptureFixture[str]) -> None:
    trace_id = "2af7651916cd43dd8448eb211c80319c"
    metadata = (("traceparent", f"00-{trace_id}-b7ad6b7169203331-01"),)

    async def scenario() -> None:
        async with running_service() as harness:
            with pytest.raises(grpc.aio.AioRpcError):
                await harness.method("Echo")(b"hi", timeout=5, metadata=metadata)

    asyncio.run(scenario())
    rejected = next(
        item for item in call_log_entries(capsys) if item["message"] == "Rejected a call"
    )
    assert rejected["trace_id"] == trace_id
    assert "tenant_id" not in rejected


def test_a_stale_tenant_does_not_leak_into_the_next_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            await harness.method("Echo")(b"hi", timeout=5, metadata=GOOD)
            with pytest.raises(grpc.aio.AioRpcError):
                await harness.method("Echo")(b"hi", timeout=5)

    asyncio.run(scenario())
    rejected = next(
        item for item in call_log_entries(capsys) if item["message"] == "Rejected a call"
    )
    assert "tenant_id" not in rejected


def test_client_span_is_the_parent_of_the_server_span() -> None:
    EXPORTER.clear()

    async def scenario() -> None:
        async with running_service() as harness:
            active = CircuitBreaker("test", BreakerConfig(slow_call_seconds=5.0))
            client = ServiceClient(active, identity_token=token)
            await client.call(harness.method("Echo"), b"hi", idempotent=True, deadline_seconds=5)

    asyncio.run(scenario())
    spans = EXPORTER.get_finished_spans()
    server = next(item for item in spans if item.name.endswith("/Echo"))
    caller = next(item for item in spans if item.name == "call test")
    assert caller.kind is trace.SpanKind.CLIENT
    assert server.parent is not None
    assert caller.context is not None
    assert server.parent.span_id == caller.context.span_id
    assert server.parent.trace_id == caller.context.trace_id
