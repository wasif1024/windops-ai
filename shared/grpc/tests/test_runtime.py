"""Exercise health, metrics and shutdown through real servers."""

import asyncio
import logging

import grpc
import httpx
import pytest
from grpc_harness import running_service, token
from grpc_health.v1 import health_pb2, health_pb2_grpc

GOOD = (("x-windops-identity", token()),)
SERVING = health_pb2.HealthCheckResponse.SERVING


async def grpc_health(channel: grpc.aio.Channel) -> int:
    stub = health_pb2_grpc.HealthStub(channel)
    response = await stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=5)
    return int(response.status)


def test_grpc_health_needs_no_identity_and_reports_serving() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            assert await grpc_health(harness.channel) == SERVING

    asyncio.run(scenario())


def test_http_health_and_metrics() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            base = f"http://127.0.0.1:{harness.runtime.http_port}"
            async with httpx.AsyncClient(base_url=base) as client:
                live = await client.get("/health/live")
                ready = await client.get("/health/ready")
                metrics = await client.get("/metrics")
            assert live.status_code == 200
            assert ready.status_code == 200
            assert metrics.status_code == 200
            assert "text/plain" in metrics.headers["content-type"]

    asyncio.run(scenario())


def test_failed_readiness_check_returns_503_while_alive() -> None:
    async def not_ready() -> bool:
        return False

    async def scenario() -> None:
        async with running_service(readiness_checks=[not_ready]) as harness:
            base = f"http://127.0.0.1:{harness.runtime.http_port}"
            async with httpx.AsyncClient(base_url=base) as client:
                assert (await client.get("/health/ready")).status_code == 503
                assert (await client.get("/health/live")).status_code == 200

    asyncio.run(scenario())


def test_crashing_readiness_check_means_not_ready() -> None:
    async def broken() -> bool:
        raise ConnectionError("database down")

    async def scenario() -> None:
        async with running_service(readiness_checks=[broken]) as harness:
            assert await harness.runtime.is_ready() is False

    asyncio.run(scenario())


def test_in_flight_call_finishes_during_shutdown() -> None:
    async def scenario() -> None:
        async with running_service() as harness:

            async def waiting_call() -> bytes:
                result: bytes = await harness.method("Echo")(b"wait", timeout=10, metadata=GOOD)
                return result

            call = asyncio.create_task(waiting_call())
            while not harness.calls:
                await asyncio.sleep(0.01)
            stopping = asyncio.create_task(harness.runtime._grpc.stop(5))  # type: ignore[union-attr]
            await asyncio.sleep(0.05)
            harness.release.set()
            assert await call == b"ok:tenant-a"
            await stopping

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_health_turns_not_serving_when_stopping() -> None:
    async def scenario() -> None:
        async with running_service() as harness:
            await harness.runtime._set_serving(False)
            assert await grpc_health(harness.channel) == health_pb2.HealthCheckResponse.NOT_SERVING
            assert await harness.runtime.is_ready() is False

    asyncio.run(scenario())


def test_lifecycle_and_health_changes_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        async with running_service():
            logging.getLogger().addHandler(caplog.handler)

    asyncio.run(scenario())
    messages = [record.message for record in caplog.records]
    assert "Service stopping" in messages
    assert "Service stopped" in messages
    assert "Health changed" in messages


def test_calls_produce_metrics_by_status() -> None:
    async def scenario() -> str:
        async with running_service() as harness:
            await harness.method("Echo")(b"hi", timeout=5, metadata=GOOD)
            with pytest.raises(grpc.aio.AioRpcError):
                await harness.method("Echo")(b"missing", timeout=5, metadata=GOOD)
            with pytest.raises(grpc.aio.AioRpcError):
                await harness.method("Echo")(b"hi", timeout=5)
            base = f"http://127.0.0.1:{harness.runtime.http_port}"
            async with httpx.AsyncClient(base_url=base) as client:
                return (await client.get("/metrics")).text

    text = asyncio.run(scenario())
    assert "windops_rpc_server_duration_seconds_count" in text
    for status in ("OK", "NOT_FOUND", "UNAUTHENTICATED"):
        assert f'rpc_status="{status}"' in text


def test_health_checks_produce_no_call_metrics() -> None:
    async def scenario() -> str:
        async with running_service() as harness:
            await grpc_health(harness.channel)
            base = f"http://127.0.0.1:{harness.runtime.http_port}"
            async with httpx.AsyncClient(base_url=base) as client:
                return (await client.get("/metrics")).text

    assert "grpc.health.v1.Health" not in asyncio.run(scenario())
