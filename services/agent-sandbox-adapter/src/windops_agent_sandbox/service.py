"""The gRPC API: list the registered tasks, and run one. Only the Agent Runtime may call it.

The runtime decides which agent may run which task, from the agent's allowlist, and records each
run (ADR 0024). So this service checks the caller's certificate, not a permission. It holds no
state: each run gets a sandbox of its own, deleted afterwards, and runs at most `concurrency` at
once, so a burst of runs cannot exhaust the Sandbox's quota.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any

from windops.agent_sandbox.v1 import agent_sandbox_pb2 as pb
from windops.agent_sandbox.v1 import agent_sandbox_pb2_grpc
from windops_auth import current_peer_service
from windops_grpc import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnavailableError,
)
from windops_resilience import BreakerOpenError, CircuitBreaker

from windops_agent_sandbox.executor import Execution, Executor, SandboxUnavailableError
from windops_agent_sandbox.tasks import Task, TaskInputError, sha256

logger = logging.getLogger("windops.agent_sandbox")
AGENT_RUNTIME = "windops-agent-runtime"
_RUN = re.compile(r"run_[a-z0-9]{20}")


def limits_message(task: Task) -> pb.Limits:
    limits = task.limits
    return pb.Limits(
        input_bytes=limits.input_bytes, seconds=limits.seconds, memory_mb=limits.memory_mb,
        output_bytes=limits.output_bytes,
    )  # fmt: skip


def task_message(task: Task) -> pb.Task:
    return pb.Task(
        name=task.name, version=task.version, description=task.description,
        input_schema_json=json.dumps(task.input_schema, sort_keys=True),
        output_schema_json=json.dumps(task.output_schema, sort_keys=True),
        code_sha256=task.code_sha256, limits=limits_message(task),
    )  # fmt: skip


def outcome(task: Task, ran: Execution) -> tuple["pb.RunStatus.ValueType", str, str]:
    """The status, the output as canonical JSON, and why it did not complete."""
    if ran.timed_out:
        return pb.RUN_STATUS_LIMIT_EXCEEDED, "", f"It ran past its {task.limits.seconds} s limit"
    if ran.out_of_memory:
        return (pb.RUN_STATUS_LIMIT_EXCEEDED, "",
                f"It ran past its {task.limits.memory_mb} MB memory limit")  # fmt: skip
    if len(ran.stdout.encode()) > task.limits.output_bytes:
        return (pb.RUN_STATUS_LIMIT_EXCEEDED, "",
                f"Its output ran past its {task.limits.output_bytes} byte limit")  # fmt: skip
    if ran.exit_code != 0:
        return pb.RUN_STATUS_FAILED, "", f"It ended with exit code {ran.exit_code}"
    try:
        value = task.checked_output(ran.stdout.strip())
    except TaskInputError as error:
        return pb.RUN_STATUS_FAILED, "", str(error)
    return pb.RUN_STATUS_COMPLETED, json.dumps(value, sort_keys=True, separators=(",", ":")), ""


class AgentSandboxService(agent_sandbox_pb2_grpc.AgentSandboxServiceServicer):
    def __init__(
        self,
        tasks: Mapping[str, Task],
        executor: Executor,
        breaker: CircuitBreaker,
        *,
        concurrency: int = 2,
        callers: Collection[str] = (AGENT_RUNTIME,),
        peer: Callable[[], str | None] = current_peer_service,
    ) -> None:
        self._tasks, self._executor, self._breaker = tasks, executor, breaker
        self._slots = asyncio.Semaphore(concurrency)
        self._callers, self._peer = frozenset(callers), peer

    def _from_runtime(self) -> None:
        if self._peer() not in self._callers:
            raise PermissionDeniedError("Only the Agent Runtime may run Sandbox tasks")

    async def ListTasks(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListTasksRequest, context: Any
    ) -> pb.ListTasksResponse:
        self._from_runtime()
        return pb.ListTasksResponse(
            tasks=[task_message(task) for _, task in sorted(self._tasks.items())]
        )

    async def RunTask(  # noqa: N802  # The name comes from the contract.
        self, request: pb.RunTaskRequest, context: Any
    ) -> pb.RunTaskResponse:
        self._from_runtime()
        task = self._tasks.get(request.task)
        if task is None:
            raise NotFoundError("No such task is registered")
        if request.run_id and not _RUN.fullmatch(request.run_id):
            raise InvalidRequestError("run_id must be a run's ID")
        try:
            given = task.checked_input(request.input_json)
        except TaskInputError as error:
            raise InvalidRequestError(str(error)) from error
        started = time.monotonic()

        async def execute() -> Execution:
            return await asyncio.to_thread(self._executor.run, task, given)

        async with self._slots:
            try:
                ran = await self._breaker.call(execute)
            except (SandboxUnavailableError, BreakerOpenError) as error:
                logger.warning(
                    "LangSmith Sandbox is unavailable",
                    extra={"task": task.name, "run_id": request.run_id,
                           "error_type": type(error).__name__},
                )  # fmt: skip
                raise UnavailableError("LangSmith Sandbox is unavailable") from error
        status, output, reason = outcome(task, ran)
        logger.info(
            "Ran a Sandbox task",
            extra={"task": task.name, "version": task.version, "run_id": request.run_id,
                   "status": pb.RunStatus.Name(status), "runtime_ms": ran.runtime_ms},
        )  # fmt: skip
        return pb.RunTaskResponse(
            status=status, output_json=output, task=task.name, version=task.version,
            code=task.code, code_sha256=task.code_sha256, input_sha256=sha256(given),
            output_sha256=sha256(output) if output else "", runtime_ms=ran.runtime_ms,
            total_ms=int((time.monotonic() - started) * 1000), reason=reason,
            limits=limits_message(task),
        )  # fmt: skip
