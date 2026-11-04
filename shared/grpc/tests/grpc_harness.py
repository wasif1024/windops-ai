"""Start a real service runtime with test methods, on free local ports."""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import grpc
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ServiceRuntime,
    Settings,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_grpc.runtime import ReadinessCheck

SECRET = "x" * 64
SERVICE = "windops.test.v1.TestService"
IDENTITY = VerifiedIdentity(subject="svc-test", tenant_id="tenant-a", user_id="user-1")

DOMAIN_ERRORS: dict[bytes, Exception] = {
    b"invalid": InvalidRequestError("Bad field"),
    b"missing": NotFoundError("No such turbine"),
    b"denied": PermissionDeniedError("Not allowed"),
    b"conflict": ConflictError("Wrong state"),
    b"unavailable": UnavailableError("Try later"),
}


def token(identity: VerifiedIdentity = IDENTITY) -> str:
    return sign_identity(identity, SECRET)


def settings() -> Settings:
    return Settings(
        environment="test",
        version="1.0.0",
        identity_secret=SECRET,
        grpc_port=0,
        http_port=0,
        shutdown_grace_seconds=5,
    )


@dataclass
class Harness:
    runtime: ServiceRuntime
    channel: grpc.aio.Channel
    calls: list[bytes] = field(default_factory=list)
    seen: list[dict[str, Any]] = field(default_factory=list)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def method(self, name: str) -> Any:
        return self.channel.unary_unary(f"/{SERVICE}/{name}")

    def stream(self, name: str) -> Any:
        return self.channel.unary_stream(f"/{SERVICE}/{name}")


@asynccontextmanager
async def running_service(
    readiness_checks: Sequence[ReadinessCheck] = (),
) -> AsyncIterator[Harness]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    holder: dict[str, Harness] = {}

    async def echo(request: bytes, context: Any) -> bytes:
        harness = holder["harness"]
        harness.calls.append(request)
        identity = current_identity()
        harness.seen.append(
            {
                "tenant": identity.tenant_id,
                "metadata": dict(context.invocation_metadata()),
            }
        )
        if request in DOMAIN_ERRORS:
            raise DOMAIN_ERRORS[request]
        if request == b"crash":
            raise RuntimeError("password=hunter2 in secret internals")
        if request == b"wait":
            await harness.release.wait()
        return b"ok:" + identity.tenant_id.encode()

    async def numbers(request: bytes, context: Any) -> AsyncIterator[bytes]:
        holder["harness"].calls.append(request)
        yield current_identity().tenant_id.encode()
        if request == b"missing":
            raise NotFoundError("Gone mid-stream")
        yield b"second"

    def register(server: grpc.aio.Server) -> None:
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    SERVICE,
                    {
                        "Echo": grpc.unary_unary_rpc_method_handler(echo),
                        "Numbers": grpc.unary_stream_rpc_method_handler(numbers),
                    },
                ),
            )
        )

    runtime = ServiceRuntime(
        "test-service",
        settings(),
        register,
        readiness_checks=readiness_checks,
        install_global_telemetry=False,
    )
    await runtime.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}")
    harness = Harness(runtime=runtime, channel=channel)
    holder["harness"] = harness
    try:
        yield harness
    finally:
        await channel.close()
        await runtime.stop()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
