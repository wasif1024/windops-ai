"""Start the Agent Runtime Service: its API, its workers, and the relay for its audit records."""

import asyncio
import contextlib
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.agent_runtime.v1 import agent_runtime_pb2_grpc
from windops.identity.v1 import identity_pb2_grpc
from windops_auth import current_identity_token
from windops_claude import load
from windops_events import EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import resilience_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_agent_runtime.identity import IdentityClient
from windops_agent_runtime.queue import Queue
from windops_agent_runtime.registry import REGISTRY
from windops_agent_runtime.service import AgentRuntimeService
from windops_agent_runtime.store import SERVICE_NAME
from windops_agent_runtime.team.sandbox import sandbox_from_environment
from windops_agent_runtime.team.standin import StandIn
from windops_agent_runtime.team.tools import McpClient, ToolBox
from windops_agent_runtime.tracing import tracer_from_environment
from windops_agent_runtime.worker import Worker, WorkerSettings
from windops_agent_runtime.workflows import ClaudeUnavailableError

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5
# Only this service's pod mounts the key, read at each call so a replaced key needs no restart.
KEY_FILE = Path("/var/run/windops/secrets/anthropic/api-key")


def anthropic_key() -> str:
    try:
        key = KEY_FILE.read_text().strip()
    except OSError:
        key = ""
    if not key:
        raise ClaudeUnavailableError("The runtime has no Anthropic key: set ANTHROPIC_API_KEY")
    return key


def _setting(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be above zero")
    return value


async def relay_events(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish queued audit records. An outage only delays them (SRS 7.7)."""
    events = None
    while events is None:
        if stop.is_set():
            return
        try:
            events = await open_events(SERVICE_NAME)
        except EventsUnavailableError as error:
            logger.warning(
                "Cannot reach the events back end yet", extra={"error_type": type(error).__name__}
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    try:
        await OutboxRelay(engine, events.publisher).run_forever(stop)
    finally:
        await events.close()


def identity_client() -> IdentityClient:
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    stubs: list[Any] = []

    def stub() -> Any:
        # A channel must be made on the running loop, so on the first call.
        if not stubs:
            channel = create_channel(os.environ["WINDOPS_IDENTITY_ADDRESS"], tls)
            stubs.append(identity_pb2_grpc.IdentityServiceStub(channel))
        return stubs[0]

    breaker = CircuitBreaker(
        "identity",
        BreakerConfig(slow_call_seconds=1.0),
        **resilience_metrics(metrics.get_meter_provider()),
    )
    return IdentityClient(
        stub,
        ServiceClient(breaker, identity_token=current_identity_token),
        os.environ["WINDOPS_IDENTITY_SECRET"],
    )


def main() -> None:
    slots = int(_setting("WINDOPS_RUNTIME_WORKER_SLOTS", 2))
    max_attempts = int(_setting("WINDOPS_RUNTIME_MAX_ATTEMPTS", 3))
    # Each slot holds a connection while it works, and the API needs a few of its own.
    engine = create_service_engine(pool_size=slots + 3, max_overflow=5, pool_pre_ping=True)
    queue = Queue(
        engine,
        lease_seconds=_setting("WINDOPS_RUNTIME_LEASE_SECONDS", 30),
        queue_limit=timedelta(hours=_setting("WINDOPS_RUNTIME_QUEUE_HOURS", 24)),
    )
    policy = load()
    logger.info(
        "Loaded the Claude policy",
        extra={"policy_version": policy.version, "policy_hash": policy.content_hash},
    )
    # A stand-in Anthropic API, on 127.0.0.1 only, for dry runs at no cost (ADR 0027).
    stand_in = StandIn() if os.environ.get("WINDOPS_AGENT_STAND_IN") == "on" else None
    worker = Worker(
        queue,
        REGISTRY,
        WorkerSettings(slots=slots, poll_seconds=_setting("WINDOPS_RUNTIME_POLL_SECONDS", 1)),
        policy=policy,
        api_key=anthropic_key,
        stand_in=(lambda: stand_in.url) if stand_in is not None else None,
        identity=identity_client(),
        # The agents call the MCP servers with the runtime's own certificate (ADR 0021).
        # Sandbox tasks through the Agent Sandbox Adapter, for agents whose allowlists name them.
        toolbox=ToolBox(
            McpClient(Path(os.environ["WINDOPS_TLS_DIR"])),
            sandbox=sandbox_from_environment(Path(os.environ["WINDOPS_TLS_DIR"])),
        ),
        # Traces go to LangSmith only redacted, and only when turned on (ADR 0023).
        tracer=tracer_from_environment(),
    )

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        agent_runtime_pb2_grpc.add_AgentRuntimeServiceServicer_to_server(
            AgentRuntimeService(engine, REGISTRY, max_attempts=max_attempts, policy=policy),
            server,
        )

    async def relay(stop: asyncio.Event) -> None:
        await relay_events(engine, stop)

    async def work(stop: asyncio.Event) -> None:
        await worker.run_forever(stop)

    async def stand_in_api(stop: asyncio.Event) -> None:
        if stand_in is not None:
            await stand_in.serve(stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            background=(relay, work, stand_in_api),
        )
    )


if __name__ == "__main__":
    main()
