"""Start the Agent Runtime Service: its API, its workers, and the relay for its audit records."""

import asyncio
import contextlib
import logging
import os
from datetime import timedelta
from pathlib import Path

import grpc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.agent_runtime.v1 import agent_runtime_pb2_grpc
from windops_claude import load
from windops_events import EventsUnavailableError, open_events
from windops_grpc import run_service
from windops_migrations import create_service_engine
from windops_outbox import OutboxRelay

from windops_agent_runtime.queue import Queue
from windops_agent_runtime.registry import REGISTRY
from windops_agent_runtime.service import AgentRuntimeService
from windops_agent_runtime.store import SERVICE_NAME
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
    worker = Worker(
        queue,
        REGISTRY,
        WorkerSettings(slots=slots, poll_seconds=_setting("WINDOPS_RUNTIME_POLL_SECONDS", 1)),
        policy=policy,
        api_key=anthropic_key,
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

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            background=(relay, work),
        )
    )


if __name__ == "__main__":
    main()
