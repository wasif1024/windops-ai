"""Run the caller and responder together, on real gRPC and HTTP servers."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from windops_grpc import ServiceRuntime, Settings
from windops_sample_caller import main as caller
from windops_sample_responder import main as responder

SECRET = "x" * 64


def settings() -> Settings:
    return Settings(
        environment="test",
        version="test",
        identity_secret=SECRET,
        grpc_port=0,
        http_port=0,
        shutdown_grace_seconds=1,
    )


@asynccontextmanager
async def running_responder() -> AsyncIterator[ServiceRuntime]:
    runtime = ServiceRuntime(
        responder.SERVICE_NAME, settings(), responder.register, install_global_telemetry=False
    )
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()


@asynccontextmanager
async def running_caller(target: str, secret: str = SECRET) -> AsyncIterator[httpx.AsyncClient]:
    runtime = ServiceRuntime(
        caller.SERVICE_NAME,
        settings(),
        caller.register,
        http_router=caller.create_router(caller.Responder(target, secret)),
        install_global_telemetry=False,
    )
    await runtime.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{runtime.http_port}", timeout=30
        ) as client:
            yield client
    finally:
        await runtime.stop()


@pytest.fixture(autouse=True)
def restore_logging() -> object:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield None
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_http_request_becomes_a_signed_grpc_call() -> None:
    async def scenario() -> None:
        async with (
            running_responder() as server,
            running_caller(f"127.0.0.1:{server.grpc_port}") as client,
        ):
            response = await client.get("/sample/echo", params={"message": "hello"})
            assert response.status_code == 200
            assert response.json() == {"message": "hello", "tenant_id": "sample"}

    asyncio.run(scenario())


def test_wrong_signing_key_is_rejected_by_the_responder() -> None:
    async def scenario() -> None:
        async with (
            running_responder() as server,
            running_caller(f"127.0.0.1:{server.grpc_port}", secret="y" * 64) as client,
        ):
            response = await client.get("/sample/echo", params={"message": "hello"})
            assert response.status_code == 503
            assert response.json()["reason"] == "call_failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("message", ["", "x" * 201])
def test_invalid_message_is_refused_before_any_call(message: str) -> None:
    async def scenario() -> None:
        async with running_caller("127.0.0.1:1") as client:
            response = await client.get("/sample/echo", params={"message": message})
            assert response.status_code == 422

    asyncio.run(scenario())


def test_outage_returns_503_then_the_breaker_opens() -> None:
    async def scenario() -> None:
        async with running_caller("127.0.0.1:1") as client:
            reasons = []
            for _ in range(6):
                response = await client.get("/sample/echo", params={"message": "hello"})
                assert response.status_code == 503
                reasons.append(response.json()["reason"])
            assert reasons[0] == "call_failed"
            assert reasons[-1] == "breaker_open"

    asyncio.run(asyncio.wait_for(scenario(), 60))


def test_caller_stays_ready_during_an_outage() -> None:
    """RES-006: a dependency outage must not make Kubernetes restart this service."""

    async def scenario() -> None:
        async with running_caller("127.0.0.1:1") as client:
            await client.get("/sample/echo", params={"message": "hello"})
            assert (await client.get("/health/ready")).status_code == 200
            assert (await client.get("/health/live")).status_code == 200

    asyncio.run(asyncio.wait_for(scenario(), 60))
