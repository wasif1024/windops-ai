"""Start the Agent Sandbox Adapter: its registered tasks, run in LangSmith Sandbox (ADR 0024)."""

import asyncio
import logging
import os
from pathlib import Path

import certifi
import grpc
from opentelemetry import metrics
from windops.agent_sandbox.v1 import agent_sandbox_pb2_grpc
from windops.tenant.v1 import tenant_pb2_grpc
from windops_auth import current_identity_token
from windops_grpc import ServiceClient, create_channel, run_service
from windops_grpc.egress import egress_switches
from windops_observability import resilience_metrics
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
        **resilience_metrics(metrics.get_meter_provider()),
    )
    executor = LangSmithExecutor(SandboxClient(api_key=key or None))
    concurrency = int(os.environ.get("WINDOPS_SANDBOX_CONCURRENCY", "2"))

    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    stubs: list[tenant_pb2_grpc.TenantServiceStub] = []

    class TenantStub:
        """Made on first use, as a channel must be made on the running loop."""

        def __getattr__(self, name: str) -> object:
            if not stubs:
                channel = create_channel(os.environ["WINDOPS_TENANT_ADDRESS"], tls)
                stubs.append(tenant_pb2_grpc.TenantServiceStub(channel))
            return getattr(stubs[0], name)

    # Each tenant may turn the Sandbox off (EGR-002, ADR 0048).
    egress = egress_switches(
        TenantStub(),
        ServiceClient(
            CircuitBreaker("tenant", BreakerConfig(slow_call_seconds=1.0),
                           **resilience_metrics(metrics.get_meter_provider())),
            identity_token=current_identity_token,
        ),
        SERVICE_NAME, os.environ["WINDOPS_IDENTITY_SECRET"],
    )  # fmt: skip

    def register(server: grpc.aio.Server) -> None:
        agent_sandbox_pb2_grpc.add_AgentSandboxServiceServicer_to_server(
            AgentSandboxService(tasks, executor, breaker, concurrency=concurrency, egress=egress),
            server,
        )

    asyncio.run(run_service(SERVICE_NAME, register))


if __name__ == "__main__":
    main()
