"""The Agent Sandbox Adapter: registered tasks, their limits, and the sandbox they run in."""

import asyncio
import json
import math
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from windops.agent_sandbox.v1 import agent_sandbox_pb2 as pb
from windops_agent_sandbox.executor import (
    DENY_ALL,
    SANDBOX_MEMORY_BYTES,
    Execution,
    LangSmithExecutor,
    SandboxUnavailableError,
    command,
    idle_seconds,
)
from windops_agent_sandbox.service import AGENT_RUNTIME, AgentSandboxService
from windops_agent_sandbox.tasks import Task, TaskInputError, load_task, registry, sha256
from windops_auth import VerifiedIdentity, identity_scope
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnavailableError,
    egress,
)
from windops_resilience import BreakerConfig, CircuitBreaker

TASKS = Path(__file__).resolve().parents[1] / "tasks"
RUN = "run_" + "a" * 20


def signal(*tones: tuple[float, float], rate: int = 512, count: int = 512) -> list[float]:
    return [
        sum(amp * math.sin(2 * math.pi * hz * t / rate) for hz, amp in tones) for t in range(count)
    ]


class Local:
    """Runs a task's code here, as the sandbox would, so its answers can be checked."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def run(self, task: Task, input_json: str) -> Execution:
        self.runs.append((task.name, input_json))
        done = subprocess.run(  # noqa: S603  # The registered task's own code.
            [sys.executable, "-I", "-c", task.code], input=input_json, capture_output=True,
            text=True, timeout=task.limits.seconds, check=False,
        )  # fmt: skip
        return Execution(exit_code=done.returncode, stdout=done.stdout, runtime_ms=5)


class Fixed:
    def __init__(self, execution: Execution | Exception) -> None:
        self.execution, self.calls = execution, 0

    def run(self, task: Task, input_json: str) -> Execution:
        self.calls += 1
        if isinstance(self.execution, Exception):
            raise self.execution
        return self.execution


def breaker() -> CircuitBreaker:
    return CircuitBreaker("langsmith-sandbox", BreakerConfig(slow_call_seconds=60, minimum_calls=2,
                                                             window_size=2))  # fmt: skip


def service(executor: Any, peer: str | None = AGENT_RUNTIME, **kwargs: Any) -> AgentSandboxService:
    return AgentSandboxService(registry(TASKS), executor, kwargs.pop("breaker", None) or breaker(),
                               peer=lambda: peer, **kwargs)  # fmt: skip


def run(svc: AgentSandboxService, task: str, given: Any) -> pb.RunTaskResponse:
    """As the Agent Runtime calls it: as itself, acting for a tenant."""
    request = pb.RunTaskRequest(task=task, input_json=json.dumps(given), run_id=RUN)
    caller = VerifiedIdentity(subject="service:agent-runtime", tenant_id="north-sea")
    with identity_scope(caller, "token"):
        return asyncio.run(svc.RunTask(request, None))


def test_the_registered_tasks_load_with_their_code_hashed() -> None:
    tasks = registry(TASKS)
    assert set(tasks) == {"vibration_spectrum", "trend_fit"}
    for task in tasks.values():
        assert task.version >= 1
        assert task.code_sha256 == sha256((TASKS / task.name / "main.py").read_text())


@pytest.mark.parametrize(
    "change",
    [
        {"limits": {"input_bytes": 1, "seconds": 999, "memory_mb": 1, "output_bytes": 1}},
        {"input_schema": {"type": "object"}},
        {"name": "other_name"},
    ],
)
def test_a_task_past_the_ceiling_or_with_an_open_schema_is_refused(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    source = TASKS / "trend_fit"
    folder = tmp_path / "trend_fit"
    folder.mkdir()
    spec = {**yaml.safe_load((source / "task.yaml").read_text()), **change}
    (folder / "task.yaml").write_text(yaml.safe_dump(spec))
    (folder / "main.py").write_text((source / "main.py").read_text())
    with pytest.raises(ValueError):
        load_task(folder)


def test_the_spectrum_task_finds_the_strongest_frequencies() -> None:
    answer = run(service(Local()), "vibration_spectrum",
                 {"sample_rate_hz": 512, "samples": signal((37, 1.0), (91, 0.4))})  # fmt: skip
    assert answer.status == pb.RUN_STATUS_COMPLETED
    peaks = json.loads(answer.output_json)["peaks"]
    assert [p["frequency_hz"] for p in peaks[:2]] == [37.0, 91.0]
    assert math.isclose(peaks[0]["amplitude"], 1.0, rel_tol=0.02)


def test_the_trend_task_projects_to_a_threshold() -> None:
    points = [{"hours": h, "value": 60 + 2 * h} for h in range(10)]
    answer = run(service(Local()), "trend_fit", {"points": points, "threshold": 80})
    found = json.loads(answer.output_json)
    assert found["slope_per_hour"] == 2.0 and found["r_squared"] == 1.0
    # The last reading, at 9 hours, is 78: two more degrees is an hour away.
    assert found["hours_to_threshold"] == 1.0
    falling = [{"hours": h, "value": 60 - h} for h in range(5)]
    found = json.loads(run(service(Local()), "trend_fit",
                           {"points": falling, "threshold": 80}).output_json)  # fmt: skip
    assert found["hours_to_threshold"] is None


def test_an_answer_says_what_ran_sbx_003() -> None:
    given = {"sample_rate_hz": 512, "samples": signal((37, 1.0))}
    answer = run(service(Local()), "vibration_spectrum", given)
    task = registry(TASKS)["vibration_spectrum"]
    canonical = json.dumps(given, sort_keys=True, separators=(",", ":"))
    assert (answer.task, answer.version) == ("vibration_spectrum", task.version)
    assert answer.code == task.code and answer.code_sha256 == task.code_sha256
    assert answer.input_sha256 == sha256(canonical)
    assert answer.output_sha256 == sha256(answer.output_json)
    assert answer.limits.seconds == task.limits.seconds and answer.runtime_ms == 5


def test_only_the_agent_runtime_may_call_it() -> None:
    svc = service(Local(), peer="windops-investigation")
    with pytest.raises(PermissionDeniedError):
        run(svc, "trend_fit", {"points": []})
    with pytest.raises(PermissionDeniedError):
        asyncio.run(svc.ListTasks(pb.ListTasksRequest(), None))


@pytest.mark.parametrize(
    ("task", "given", "error"),
    [
        ("run_shell", {}, NotFoundError),
        ("trend_fit", {"points": [{"hours": 0, "value": 1}]}, InvalidRequestError),
        ("trend_fit", {"points": [{"hours": 0, "value": 1}] * 3, "code": "import os"},
         InvalidRequestError),
        ("vibration_spectrum", {"sample_rate_hz": 512, "samples": [0.5] * 9000},
         InvalidRequestError),
    ],
)  # fmt: skip
def test_unknown_tasks_and_bad_input_never_reach_a_sandbox(
    task: str, given: Any, error: type[Exception]
) -> None:
    local = Local()
    with pytest.raises(error):
        run(service(local), task, given)
    assert local.runs == []


def test_input_over_its_limit_is_refused_before_parsing() -> None:
    task = registry(TASKS)["trend_fit"]
    with pytest.raises(TaskInputError, match="bytes"):
        task.checked_input(" " * (task.limits.input_bytes + 1))


@pytest.mark.parametrize(
    ("execution", "status", "reason"),
    [
        (Execution(-1, "", 20_000, timed_out=True), pb.RUN_STATUS_LIMIT_EXCEEDED, "s limit"),
        (Execution(137, "", 10, out_of_memory=True), pb.RUN_STATUS_LIMIT_EXCEEDED, "memory"),
        (Execution(0, "x" * 5000, 10), pb.RUN_STATUS_LIMIT_EXCEEDED, "byte limit"),
        (Execution(1, "", 10), pb.RUN_STATUS_FAILED, "exit code 1"),
        (Execution(0, '{"peaks": "none"}', 10), pb.RUN_STATUS_FAILED, "schema"),
        (Execution(0, "not json", 10), pb.RUN_STATUS_FAILED, "not JSON"),
    ],
)
def test_a_run_past_a_limit_or_with_bad_output_returns_no_output(
    execution: Execution, status: "pb.RunStatus.ValueType", reason: str
) -> None:
    answer = run(service(Fixed(execution)), "vibration_spectrum",
                 {"sample_rate_hz": 512, "samples": signal((37, 1.0))})  # fmt: skip
    assert answer.status == status and reason in answer.reason
    assert answer.output_json == "" and answer.output_sha256 == ""


def test_an_unavailable_sandbox_is_unavailable_and_opens_the_breaker() -> None:
    fixed = Fixed(SandboxUnavailableError("SandboxConnectionError"))
    svc = service(fixed, breaker=breaker())
    given = {"points": [{"hours": h, "value": h} for h in range(3)]}
    for _ in range(3):
        with pytest.raises(UnavailableError):
            run(svc, "trend_fit", given)
    # Two failures open the breaker: the third never tried the Sandbox.
    assert fixed.calls == 2


def test_a_sandbox_turned_off_runs_nothing_and_never_opens_the_breaker() -> None:
    """EGR-002: refused for that tenant, as a refusal, so other tenants keep their Sandbox."""
    local = Local()
    svc = service(local, egress=egress.Fixed({"langsmith_sandbox"}))
    given = {"points": [{"hours": h, "value": h} for h in range(3)]}
    for _ in range(3):
        with pytest.raises(ConflictError, match="EGR-002"):
            run(svc, "trend_fit", given)
    assert svc._breaker.state.value == "closed"


def test_list_tasks_gives_schemas_limits_and_hashes() -> None:
    listed = asyncio.run(service(Local()).ListTasks(pb.ListTasksRequest(), None)).tasks
    assert [t.name for t in listed] == ["trend_fit", "vibration_spectrum"]
    for task in listed:
        assert json.loads(task.input_schema_json)["additionalProperties"] is False
        assert len(task.code_sha256) == 64 and task.limits.memory_mb > 0


def test_the_command_runs_clean_within_its_limits() -> None:
    task = registry(TASKS)["trend_fit"]
    line = command(task)
    assert "env -i PATH=" in line and "python3 -I" in line
    assert f"timeout --kill-after=2 {task.limits.seconds} " in line
    assert f"ulimit -v {task.limits.memory_mb * 1024}" in line
    assert f"head -c {task.limits.output_bytes + 1}" in line


class FakeSandbox:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.files: dict[str, str] = {}
        self.result, self.error = result, error
        self.ran: list[tuple[str, int]] = []

    def write(self, path: str, content: str) -> None:
        self.files[path] = content

    def run(self, line: str, *, timeout: int, cwd: str) -> Any:
        self.ran.append((line, timeout))
        if self.error is not None:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, sandbox: FakeSandbox, create_error: Exception | None = None) -> None:
        self.sandbox, self.create_error = sandbox, create_error
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def create_sandbox(self, **kwargs: Any) -> FakeSandbox:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(kwargs)
        return self.sandbox

    def delete_sandbox(self, name: str) -> None:
        self.deleted.append(name)


def test_each_run_gets_a_new_sandbox_with_no_secrets_or_network_and_deletes_it() -> None:
    from langsmith.sandbox import ExecutionResult

    task = registry(TASKS)["trend_fit"]
    sandbox = FakeSandbox(ExecutionResult(stdout='{"a": 1}', stderr="", exit_code=0))
    client = FakeClient(sandbox)
    ran = LangSmithExecutor(client).run(task, '{"points": []}')
    (created,) = client.created
    assert created["proxy_config"] == DENY_ALL and created["persistent"] is False
    # The least LangSmith allows; the task's own limit is its process's, by ulimit.
    assert created["vcpus"] == 1 and created["mem_bytes"] == SANDBOX_MEMORY_BYTES
    # Nothing is mounted, and no secret or variable is given.
    assert not {"mount_config", "access_delegation", "run_config", "env"} & set(created)
    assert set(sandbox.files.values()) == {task.code, '{"points": []}'}
    assert client.deleted == [created["name"]]
    # LangSmith takes the idle limit only in whole minutes, for every registered task.
    for registered in registry(TASKS).values():
        assert idle_seconds(registered) % 60 == 0
        assert idle_seconds(registered) >= registered.limits.seconds * 4
    assert ran.exit_code == 0 and ran.stdout == '{"a": 1}'
    assert sandbox.ran[0][1] == task.limits.seconds + 10


def test_a_timed_out_or_failed_sandbox_is_still_deleted() -> None:
    from langsmith.sandbox import CommandTimeoutError, SandboxConnectionError

    task = registry(TASKS)["trend_fit"]
    client = FakeClient(FakeSandbox(error=CommandTimeoutError("too long")))
    assert LangSmithExecutor(client).run(task, "{}").timed_out
    assert len(client.deleted) == 1
    client = FakeClient(FakeSandbox(error=SandboxConnectionError("gone")))
    with pytest.raises(SandboxUnavailableError):
        LangSmithExecutor(client).run(task, "{}")
    assert len(client.deleted) == 1
    client = FakeClient(FakeSandbox(), create_error=SandboxConnectionError("gone"))
    with pytest.raises(SandboxUnavailableError):
        LangSmithExecutor(client).run(task, "{}")
    assert client.deleted == []


def test_a_task_stopped_by_its_time_limit_is_reported_as_such() -> None:
    from langsmith.sandbox import ExecutionResult

    task = registry(TASKS)["trend_fit"]
    for code in (124, -1):
        client = FakeClient(FakeSandbox(ExecutionResult(stdout="", stderr="", exit_code=code)))
        assert LangSmithExecutor(client).run(task, "{}").timed_out


def test_out_of_memory_is_reported_as_such() -> None:
    from langsmith.sandbox import ExecutionResult

    task = replace(registry(TASKS)["trend_fit"])
    client = FakeClient(FakeSandbox(ExecutionResult(stdout="", stderr="MemoryError", exit_code=1)))
    assert LangSmithExecutor(client).run(task, "{}").out_of_memory
