"""Start the Agent Sandbox Adapter: its registered tasks, run in LangSmith Sandbox (ADR 0024)."""

import asyncio
import logging
import os
from pathlib import Path

import certifi
import grpc
from opentelemetry import metrics
from windops.agent_sandbox.v1 import agent_sandbox_pb2_grpc
from windops_grpc import run_service
from windops_observability import breaker_state_metrics
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_agent_sandbox.executor import LangSmithExecutor
from windops_agent_sandbox.service import AgentSandboxService
from windops_agent_sandbox.tasks import registry

SERVICE_NAME = "agent-sandbox-adapter"
logger = logging.getLogger("windops.service")
# Only this service's pod mounts the key: the sandboxes it creates never see it (SBX-002).
KEY_FILE = Path("/var/run/windops/secrets/langsmith/api-key")


def main() -> None:
    from langsmith.sandbox import SandboxClient

    tasks = registry()
    logger.info(
        "Loaded the registered Sandbox tasks",
        extra={"tasks": {name: f"{t.version}:{t.code_sha256[:12]}" for name, t in tasks.items()}},
    )
    # The Sandbox's client uses Python's default certificate store, which the slim image lacks.
    # The public CA bundle is named for it; verification is never turned off.
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""
    if not key:
        # The service still starts and answers ListTasks; every run is unavailable.
        logger.warning("No LangSmith key is mounted: every Sandbox run will be unavailable")
    breaker = CircuitBreaker(
        "langsmith-sandbox",
        # A sandbox takes seconds to create, and a task up to its own limit.
        BreakerConfig(slow_call_seconds=180, minimum_calls=4, window_size=10),
        on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
    )
    executor = LangSmithExecutor(SandboxClient(api_key=key or None))
    concurrency = int(os.environ.get("WINDOPS_SANDBOX_CONCURRENCY", "2"))

    def register(server: grpc.aio.Server) -> None:
        agent_sandbox_pb2_grpc.add_AgentSandboxServiceServicer_to_server(
            AgentSandboxService(tasks, executor, breaker, concurrency=concurrency), server
        )

    asyncio.run(run_service(SERVICE_NAME, register))


if __name__ == "__main__":
    main()
