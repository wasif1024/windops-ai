"""Where a task's code runs: a new LangSmith Sandbox for each run (SBX-002, ADR 0015).

Each sandbox is created for one run and deleted after it, whatever happened:
- **No credentials:** the sandbox is given no secret, mount or variable, and the task runs under
  `env -i`, with only a PATH. The LangSmith key stays in this service.
- **No network it can name:** its proxy allows one destination that does not exist, so every
  other destination is denied. LangSmith's managed allowlist stays open on this plan, which
  ADR 0015 accepted with compensating controls.
- **Limits:** the sandbox has one vCPU and the least memory LangSmith allows with it, 2 GiB. The
  task's own memory limit is its process's address-space limit, set by `ulimit -v`. The command
  is stopped at the task's time by `timeout`, and its output is cut one byte past the task's limit.

The task's error output stays here: it may echo its input, so it is never returned or logged.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from windops_agent_sandbox.tasks import Task

logger = logging.getLogger("windops.agent_sandbox")
# The only destination the proxy allows, which cannot resolve, so it denies everything it can.
DENY_ALL = {"access_control": {"allow_list": ["egress-denied.invalid"]}}
WORKDIR = "/tmp"  # noqa: S108  # Inside the sandbox, which is deleted after the run.
CREATE_SECONDS = 90
# LangSmith requires a sandbox's memory within 50% of 4 GiB per vCPU: 2 GiB is the least for one.
SANDBOX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
BACKSTOP_SECONDS = 10
# timeout's own exit codes, and the Sandbox client's when its command outlived it.
TIMED_OUT = frozenset({124, -1})
_MEMORY = re.compile(r"MemoryError|Cannot allocate memory")


class SandboxUnavailableError(ConnectionError):
    """LangSmith Sandbox could not be reached, or would not create a sandbox."""


@dataclass(frozen=True)
class Execution:
    exit_code: int
    stdout: str
    runtime_ms: int
    timed_out: bool = False
    out_of_memory: bool = False


class Executor(Protocol):
    def run(self, task: Task, input_json: str) -> Execution:
        """Run the task's code on the input, in a sandbox of its own. Blocking."""


def command(task: Task) -> str:
    """The task's command: a clean environment, its memory and output limits, input on stdin."""
    kilobytes = task.limits.memory_mb * 1024
    return (
        "set -o pipefail; "
        f"ulimit -v {kilobytes}; "
        f"timeout --kill-after=2 {task.limits.seconds} "
        f"env -i PATH=/usr/local/bin:/usr/bin:/bin HOME={WORKDIR} "
        f"python3 -I {WORKDIR}/main.py < {WORKDIR}/input.json "
        f"| head -c {task.limits.output_bytes + 1}"
    )


class LangSmithExecutor:
    def __init__(self, client: Any) -> None:
        self._client = client

    def run(self, task: Task, input_json: str) -> Execution:
        from langsmith.sandbox import CommandTimeoutError, SandboxClientError

        name = f"windops-{task.name.replace('_', '-')}-{uuid.uuid4().hex[:12]}"
        try:
            sandbox = self._client.create_sandbox(
                name=name, persistent=False, timeout=CREATE_SECONDS,
                idle_ttl_seconds=idle_seconds(task), delete_after_stop_seconds=60,
                vcpus=1, mem_bytes=SANDBOX_MEMORY_BYTES, proxy_config=DENY_ALL,
            )  # fmt: skip
        except SandboxClientError as error:
            raise SandboxUnavailableError(type(error).__name__) from error
        try:
            sandbox.write(f"{WORKDIR}/main.py", task.code)
            sandbox.write(f"{WORKDIR}/input.json", input_json)
            started = time.monotonic()
            try:
                # The command's own timeout stops the task; this one only backs it up.
                result = sandbox.run(
                    command(task), timeout=task.limits.seconds + BACKSTOP_SECONDS, cwd=WORKDIR
                )
            except CommandTimeoutError:
                return Execution(-1, "", _since(started), timed_out=True)
            if result.exit_code in TIMED_OUT:
                return Execution(result.exit_code, "", _since(started), timed_out=True)
            return Execution(
                exit_code=result.exit_code, stdout=result.stdout, runtime_ms=_since(started),
                out_of_memory=result.exit_code == 137 or bool(_MEMORY.search(result.stderr or "")),
            )  # fmt: skip
        except SandboxClientError as error:
            raise SandboxUnavailableError(type(error).__name__) from error
        finally:
            try:
                self._client.delete_sandbox(name)
            except Exception as error:  # Its idle limit deletes it anyway.
                logger.warning(
                    "Could not delete a sandbox: its idle limit will",
                    extra={"sandbox": name, "error_type": type(error).__name__},
                )


def idle_seconds(task: Task) -> int:
    """How long an unused sandbox lives, if its delete fails: four times the task's time, in whole
    minutes, as LangSmith requires."""
    return 60 * max(1, -(-task.limits.seconds * 4 // 60))


def _since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
