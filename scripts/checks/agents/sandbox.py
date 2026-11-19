#!/usr/bin/env python3
"""Prove the Agent Sandbox Adapter on the cluster and against LangSmith Sandbox (M8.10).

SBX-001 to SBX-003, ADR 0024. Three parts:

- **The adapter, called directly:** only the Agent Runtime's certificate is let in; its
  registered tasks are exactly the repository's, by code hash; an unknown task, and input that
  breaks a task's schema or carries code, are refused; both tasks answer correctly in a real
  sandbox, with their version, code, hashes and runtime (SBX-001, SBX-003).
- **The adapter's own executor, against LangSmith Sandbox:** a runaway task is stopped by its time,
  memory and output limits, and a sandbox made as the adapter makes them holds no credentials
  and reaches no ordinary website (SBX-001, SBX-002).
- **Through the runtime:** a traced rehearsal in which the Reliability Engineer runs the spectrum
  task. Its trace in LangSmith holds what ran, and the runtime's audit record names the agent,
  the user and the run's correlation ID (SBX-003, OBS-008).

Nothing is asked of Claude. It costs a few sandboxes' seconds of LangSmith compute. Run
`make secrets` with LANGSMITH_API_KEY in .env first.
"""

import base64
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import httpx
import jwt
from windops.agent_sandbox.v1 import agent_sandbox_pb2 as pb
from windops.agent_sandbox.v1 import agent_sandbox_pb2_grpc
from windops_agent_sandbox.executor import LangSmithExecutor
from windops_agent_sandbox.service import outcome
from windops_agent_sandbox.tasks import Limits, Task, registry

from scripts.checks.agents.runtime import investigation_bundle
from scripts.checks.agents.tools import Cases
from scripts.checks.agents.trace import langsmith, read_trace, start
from scripts.checks.fleet.asset import Caller, kubectl
from scripts.checks.identity.identity import Realm
from scripts.checks.identity.token_exchange import TENANT, check, signing_key
from scripts.lib import settings
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

APPLICATION = "windops-application"
DEPLOYMENT = "windops-agent-sandbox-adapter"
HOST = "windops-agent-sandbox-adapter.windops-application.svc.cluster.local"
RUNTIME = "service:agent-runtime"
TASKS = Path(__file__).resolve().parents[3] / "services/agent-sandbox-adapter/tasks"
RATE = 512


def runtime_bundle() -> ClientBundle:
    """The Agent Runtime's own certificate, as it would call."""
    data = json.loads(
        kubectl("get", "secret", "windops-agent-runtime-tls", "--output=json",
                namespace=APPLICATION).stdout
    )["data"]  # fmt: skip
    return ClientBundle(*(base64.b64decode(data[k]) for k in ("tls.crt", "tls.key", "ca.crt")))


@contextmanager
def adapter(bundle: ClientBundle, key: str) -> Iterator[Caller]:
    with (
        port_forward(f"deployment/{DEPLOYMENT}", APPLICATION, 50051) as port,
        blocking_channel(port, HOST, bundle) as channel,
    ):
        yield Caller(agent_sandbox_pb2_grpc.AgentSandboxServiceStub(channel), key)


def run_task(api: Caller, task: str, given: Any) -> pb.RunTaskResponse:
    request = pb.RunTaskRequest(task=task, input_json=json.dumps(given))
    response: pb.RunTaskResponse = api.stub.RunTask(
        request, metadata=api.metadata(RUNTIME, TENANT, level=None), timeout=240
    )
    return response


def vibration() -> list[float]:
    return [math.sin(2 * math.pi * 37 * t / RATE) + 0.4 * math.sin(2 * math.pi * 91 * t / RATE)
            for t in range(RATE)]  # fmt: skip


def prove_callers(key: str) -> None:
    refused = []
    for bundle in (client_bundle(), investigation_bundle()):
        with adapter(bundle, key) as stranger:
            try:
                stranger.call("ListTasks", pb.ListTasksRequest(), RUNTIME, TENANT, level=None)
                refused.append(grpc.StatusCode.OK)
            except grpc.RpcError as error:
                refused.append(error.code())
    check(
        refused == [grpc.StatusCode.PERMISSION_DENIED] * 2,
        "Any certificate but the Agent Runtime's is refused, the Investigation Service's too",
    )


def prove_tasks(key: str) -> None:
    local = registry(TASKS)
    with adapter(runtime_bundle(), key) as api:
        listed = api.call("ListTasks", pb.ListTasksRequest(), RUNTIME, TENANT, level=None).tasks
        served = {t.name: t.code_sha256 for t in listed}
        check(
            served == {name: task.code_sha256 for name, task in local.items()},
            f"Its registered tasks are the repository's, by code hash: {sorted(served)} (SBX-001)",
        )
        codes = []
        for task, given in (
            ("run_shell", {}),
            ("trend_fit", {"points": [{"hours": 0, "value": 1}] * 3, "code": "import os"}),
            ("vibration_spectrum", {"sample_rate_hz": RATE, "samples": [0.0] * 9000}),
        ):
            try:
                run_task(api, task, given)
                codes.append(grpc.StatusCode.OK)
            except grpc.RpcError as error:
                codes.append(error.code())
        check(
            codes == [grpc.StatusCode.NOT_FOUND] + [grpc.StatusCode.INVALID_ARGUMENT] * 2,
            "An unknown task, input carrying code, and input over its limit are refused (SBX-001)",
        )
        spectrum = run_task(api, "vibration_spectrum", {"sample_rate_hz": RATE,
                                                        "samples": vibration()})  # fmt: skip
        peaks = [p["frequency_hz"] for p in json.loads(spectrum.output_json or "{}")
                 .get("peaks", [])][:2]  # fmt: skip
        spec = local["vibration_spectrum"]
        check(
            spectrum.status == pb.RUN_STATUS_COMPLETED and peaks == [37.0, 91.0]
            and spectrum.code == spec.code and spectrum.version == spec.version
            and len(spectrum.input_sha256) == 64 and len(spectrum.output_sha256) == 64,
            f"The spectrum task ran in a sandbox: peaks {peaks} Hz, in {spectrum.runtime_ms} ms "
            f"({spectrum.total_ms} ms with the sandbox), with its code, version and hashes "
            "(SBX-003)",
        )  # fmt: skip
        points = [{"hours": h, "value": 60 + 2 * h} for h in range(10)]
        trend = run_task(api, "trend_fit", {"points": points, "threshold": 80})
        fitted = json.loads(trend.output_json or "{}")
        check(
            trend.status == pb.RUN_STATUS_COMPLETED and fitted.get("hours_to_threshold") == 1.0,
            f"The trend task ran: {fitted.get('slope_per_hour')} per hour, at the threshold in "
            f"{fitted.get('hours_to_threshold')} h",
        )


def probe(name: str, code: str, **limits: int) -> Task:
    """An unregistered task, run only here, by the adapter's own executor."""
    bounds = {"input_bytes": 1024, "seconds": 10, "memory_mb": 256, "output_bytes": 256} | limits
    return Task(name=name, version=0, description=name, code=code, code_sha256="",
                limits=Limits(**bounds), input_schema={}, output_schema={})  # fmt: skip


SPIN = "while True:\n    pass\n"
GREEDY = "held = bytearray(1 << 30)\nprint(len(held))\n"
CHATTY = "print('x' * 100000)\n"
LOOK = """
import json, os, urllib.request
try:
    urllib.request.urlopen("https://example.com/", timeout=10)
    reached = True
except Exception:
    reached = False
print(json.dumps({"names": sorted(os.environ), "values": "\\n".join(os.environ.values()),
                  "reached": reached}))
"""


def prove_executor(secrets_held: list[str]) -> None:
    from langsmith.sandbox import SandboxClient

    executor = LangSmithExecutor(SandboxClient(api_key=settings.load()["LANGSMITH_API_KEY"]))
    stopped = {}
    for name, code in (("spin", SPIN), ("greedy", GREEDY), ("chatty", CHATTY)):
        task = probe(name, code, seconds=5)
        status, _, reason = outcome(task, executor.run(task, "{}"))
        stopped[name] = (pb.RunStatus.Name(status), reason)
    check(
        all(status == "RUN_STATUS_LIMIT_EXCEEDED" for status, _ in stopped.values()),
        "A runaway task is stopped at its limits: "
        + "; ".join(f"{n}: {reason}" for n, (_, reason) in stopped.items())
        + " (SBX-001)",
    )
    task = probe("look", LOOK, seconds=30, output_bytes=8192)
    ran = executor.run(task, "{}")
    seen = json.loads(ran.stdout.strip().splitlines()[-1]) if ran.exit_code == 0 else {}
    names = seen.get("names", [])
    carried = [s for s in secrets_held if s and s in seen.get("values", "")]
    check(
        bool(names) and set(names) <= {"PATH", "HOME", "PWD", "SHLVL", "_", "LC_CTYPE"}
        and not carried,
        f"A task's sandbox holds no credentials: its environment is {names}, and no key of this "
        "machine's is in it (SBX-002)",
    )  # fmt: skip
    check(seen.get("reached") is False, "It reaches no ordinary website (SBX-002)")


def audited(run_id: str, since: datetime) -> list[str]:
    statement = (
        "SELECT set_config('windops.all_tenants', 'on', false); SELECT actor_id || '|' || "  # noqa: S608  # The runtime's own ID, and a clock reading.
        "on_behalf_of || '|' || resource_id FROM audit_records WHERE action = "
        f"'agent_run.sandbox.vibration_spectrum' AND correlation_id = '{run_id}' "
        f"AND recorded_at >= '{since.isoformat()}'"
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-data", "exec",
             "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
             "--no-align", "--username=windops_audit", "--dbname=windops_audit",
             "--set=ON_ERROR_STOP=1", f"--command={statement}"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        rows = [line for line in result.stdout.strip().splitlines()[1:] if line]
        if rows:
            return rows
        time.sleep(3)
    return []


def prove_through_runtime(realm: Realm, key: str, cases: Cases) -> None:
    client, project = langsmith()
    started = datetime.now(UTC)
    run_id, _, user = start(realm, key, cases, title="Sandbox check", sandbox=True)
    _, runs = read_trace(client, project, run_id)
    tools = [r for r in runs if r.name == "sandbox_vibration_spectrum" and r.outputs]
    artifact = ((tools[0].outputs or {}).get("output") or {}).get("artifact", {}) if tools else {}
    task = registry(TASKS)["vibration_spectrum"]
    check(
        artifact.get("code_sha256") == task.code_sha256
        and artifact.get("version") == task.version
        and artifact.get("status") == "completed"
        and len(artifact.get("input_sha256", "")) == 64
        and "runtime_ms" in artifact,
        "The run's trace holds what ran: the task's version, code and hashes, and its runtime "
        "(SBX-003)",
    )
    rows = audited(run_id, started)
    check(
        rows == [f"agent:reliability-engineer|{user}|vibration_spectrum@{task.version}"],
        "The runtime audited it as the Reliability Engineer, on the user's behalf, with the run's "
        "ID as its correlation ID (OBS-008)",
    )


def main() -> int:
    values = settings.load()
    if not values.get("LANGSMITH_API_KEY"):
        print("LANGSMITH_API_KEY is not set. Add it to .env", file=sys.stderr)
        return 1
    # The Sandbox's client uses Python's default certificate store; name the public bundle.
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    try:
        key = signing_key()
        prove_callers(key)
        prove_tasks(key)
        prove_executor([values["LANGSMITH_API_KEY"], values.get("ANTHROPIC_API_KEY", "")])
        with ExitStack() as stack:
            public = stack.enter_context(keycloak_public())
            cases = Cases(key)
            stack.callback(cases.close)
            prove_through_runtime(Realm(public), key, cases)
    except (RuntimeError, OSError, KeyError, grpc.RpcError, httpx.HTTPError,
            jwt.PyJWTError) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Sandbox check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
