"""Agents' Sandbox tools: registered analysis tasks, through the Agent Sandbox Adapter (ADR 0024).

An agent whose allowlist names `sandbox.<task>` gets a tool for that task alone. Its input schema
is the task's own, read from the adapter once per process; the agent gives input, never code.
Each call:

- **answers the agent** with the task's output, labelled supporting analysis, which is never the
  input to a deterministic rule (SRS 5.9);
- **records in the trace** what ran: the task's version, its code and code hash, the input and
  output hashes, the limits and the runtime (SBX-003). They go in the tool message's artifact,
  which the trace keeps and the model never sees;
- **is audited** by the runtime, as the agent, on the user's behalf, with the run's ID as the
  correlation ID (OBS-008).

If the Sandbox is unavailable, the agent is told so and carries on without it (SRS 7.7).
"""

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import grpc
from langchain_core.tools import BaseTool, StructuredTool
from windops.agent_sandbox.v1 import agent_sandbox_pb2 as pb
from windops_grpc import acting_as_service
from windops_resilience import BreakerOpenError

from windops_agent_runtime.store import SERVICE_NAME
from windops_agent_runtime.team import counted

PREFIX = "sandbox."
LABEL = "Supporting analysis from a registered Sandbox task. Never a deterministic rule's input."
UNAVAILABLE = "Sandbox analysis is unavailable right now. Carry on without it, and say so."


class SandboxUnavailableError(Exception):
    """The Agent Sandbox Adapter, or LangSmith Sandbox behind it, cannot be reached."""


@dataclass(frozen=True)
class SandboxUse:
    agent: str
    task: str
    version: int
    status: str


Record = Callable[[SandboxUse], Awaitable[None]]


@dataclass(frozen=True)
class SandboxRun:
    """Which run the calls are part of, and where their use is recorded."""

    run_id: str
    tenant_id: str
    record: Record


class SandboxApi(Protocol):
    async def list_tasks(self, tenant: str) -> list[pb.Task]: ...

    async def run_task(
        self, tenant: str, task: str, input_json: str, run_id: str
    ) -> pb.RunTaskResponse:
        """Raises SandboxUnavailableError when it cannot be reached."""


def is_sandbox(scope: str) -> bool:
    return scope.startswith(PREFIX)


def _status(response: pb.RunTaskResponse) -> str:
    return pb.RunStatus.Name(response.status).removeprefix("RUN_STATUS_").lower()


class SandboxTasks:
    """Builds agents' Sandbox tools, with the registered tasks read once per process."""

    def __init__(self, api: SandboxApi) -> None:
        self._api = api
        self._tasks: dict[str, pb.Task] = {}

    async def _task(self, name: str, tenant: str) -> pb.Task:
        if name not in self._tasks:
            self._tasks = {task.name: task for task in await self._api.list_tasks(tenant)}
        if name not in self._tasks:
            raise ValueError(f"The Agent Sandbox Adapter has no task {name}")
        return self._tasks[name]

    async def tool(
        self,
        agent: str,
        scope: str,
        run: SandboxRun,
        before_call: Callable[[str], None] | None = None,
        after_call: Callable[[str, str], None] | None = None,
    ) -> BaseTool:
        task = await self._task(scope.removeprefix(PREFIX), run.tenant_id)
        name = f"sandbox_{task.name}"
        api = self._api

        async def call(**arguments: Any) -> tuple[str, dict[str, Any]]:
            if before_call is not None:
                before_call(name)
            labels = {"agent": agent, "server": "sandbox", "tool": task.name}
            try:
                response = await api.run_task(
                    run.tenant_id, task.name, json.dumps(arguments), run.run_id
                )
            except SandboxUnavailableError:
                counted.tool_calls.add(1, {**labels, "outcome": "error"})
                await run.record(SandboxUse(agent, task.name, task.version, "unavailable"))
                return json.dumps({"status": "unavailable", "note": UNAVAILABLE}), {
                    "task": task.name, "status": "unavailable",
                }  # fmt: skip
            status = _status(response)
            counted.tool_calls.add(
                1, {**labels, "outcome": "ok" if status == "completed" else "error"}
            )
            await run.record(SandboxUse(agent, task.name, response.version, status))
            content = json.dumps(
                {"status": status, "task": task.name, "version": response.version, "label": LABEL,
                 "output": json.loads(response.output_json) if response.output_json else None,
                 "reason": response.reason or None},
            )  # fmt: skip
            if after_call is not None:
                after_call(name, content)
            # What ran, for the trace only (SBX-003).
            return content, {
                "task": task.name, "version": response.version, "status": status,
                "code": response.code, "code_sha256": response.code_sha256,
                "input_sha256": response.input_sha256, "output_sha256": response.output_sha256,
                "runtime_ms": response.runtime_ms, "total_ms": response.total_ms,
                "limits": {"input_bytes": response.limits.input_bytes,
                           "seconds": response.limits.seconds,
                           "memory_mb": response.limits.memory_mb,
                           "output_bytes": response.limits.output_bytes},
            }  # fmt: skip

        return StructuredTool(
            name=name,
            description=f"{task.description} {LABEL}",
            args_schema=json.loads(task.input_schema_json),
            coroutine=call,
            response_format="content_and_artifact",
        )


class GrpcSandbox:
    """The Agent Sandbox Adapter, over mutual TLS, through its circuit breaker."""

    def __init__(self, stub: Callable[[], Any], client: Any, secret: str) -> None:
        self._stub, self._client, self._secret = stub, client, secret

    async def list_tasks(self, tenant: str) -> list[pb.Task]:
        # As itself, by its certificate, in the run's tenant.
        with acting_as_service(SERVICE_NAME, tenant, self._secret):
            response = await self._client.call(
                self._stub().ListTasks, pb.ListTasksRequest(), idempotent=True,
                deadline_seconds=10,
            )  # fmt: skip
        return list(response.tasks)

    async def run_task(
        self, tenant: str, task: str, input_json: str, run_id: str
    ) -> pb.RunTaskResponse:
        request = pb.RunTaskRequest(task=task, input_json=input_json, run_id=run_id)
        try:
            with acting_as_service(SERVICE_NAME, tenant, self._secret):
                # Long enough to create a sandbox and run a task to its own limit.
                return await self._client.call(  # type: ignore[no-any-return]
                    self._stub().RunTask, request, idempotent=False, deadline_seconds=240
                )
        except BreakerOpenError as error:
            raise SandboxUnavailableError("breaker open") from error
        except grpc.aio.AioRpcError as error:
            if error.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                raise SandboxUnavailableError(error.code().name) from error
            raise


def sandbox_from_environment(tls: Path) -> SandboxTasks | None:
    """The adapter, if its address is set. Without it, agents get no Sandbox tools."""
    address = os.environ.get("WINDOPS_AGENT_SANDBOX_ADDRESS", "")
    if not address:
        return None
    from opentelemetry import metrics
    from windops.agent_sandbox.v1 import agent_sandbox_pb2_grpc
    from windops_auth import current_identity_token
    from windops_grpc import ServiceClient, create_channel
    from windops_observability import breaker_state_metrics
    from windops_resilience import BreakerConfig, CircuitBreaker

    stubs: list[Any] = []

    def stub() -> Any:
        # A channel must be made on the running loop, so on the first call.
        if not stubs:
            channel = create_channel(address, tls)
            stubs.append(agent_sandbox_pb2_grpc.AgentSandboxServiceStub(channel))
        return stubs[0]

    breaker = CircuitBreaker(
        "agent-sandbox-adapter",
        BreakerConfig(slow_call_seconds=180, minimum_calls=4, window_size=10),
        on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
    )
    client = ServiceClient(breaker, identity_token=current_identity_token)
    return SandboxTasks(GrpcSandbox(stub, client, os.environ["WINDOPS_IDENTITY_SECRET"]))
