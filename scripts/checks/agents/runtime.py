#!/usr/bin/env python3
"""Prove the Agent Runtime on the cluster: runs as queued jobs, on worker replicas (M8.2).

Run `make migrate` and the service first. The check runs the runtime as two replicas, with a
10-second lease, and restores both afterwards. It proves:

- only the Investigation Service's certificate is let in, and the tenant comes from the identity;
- a run is queued at once, and the same idempotency key returns the same run;
- both replicas take runs from the one queue, and never the same run (ARC-006);
- a worker that stops renewing its lease, frozen as a crash would leave it, has its run taken over
  by the other replica, after its last completed step, as the next attempt. Unfrozen, it finds the
  lease gone and changes nothing (ARC-007);
- a worker shut down hands its run back at once, without spending an attempt;
- a running run is cancelled, and a failing one stops after three attempts (FR-INV-007);
- another tenant never sees a run, and PostgreSQL hides every run from a query naming no tenant;
- a completed step never changes, and queuing and each run's end reach the audit trail.

Runs are made in the tenant `runtime-check`. They stay, as completed steps never change.
"""

import base64
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import grpc
from windops.agent_runtime.v1 import agent_runtime_pb2 as pb
from windops.agent_runtime.v1 import agent_runtime_pb2_grpc
from windops.audit.v1 import audit_pb2, audit_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, check, kubectl, signing_key
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
DEPLOYMENT = "windops-agent-runtime"
CONTAINER = "agent-runtime"
HOST = "windops-agent-runtime.windops-application.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
DATABASE = "windops_agent_runtime"
TENANT, OTHER = "runtime-check", "runtime-check-other"
INVESTIGATION = "service:investigation"
AUDITOR = "runtime-check-auditor"
LEASE_SECONDS = 10
CASE = "case_" + "c" * 20
SNAPSHOT = "evs_" + "d" * 20
RUN = str(int(time.time()))
RUN_ID = re.compile(r"run_[a-z0-9]{20}")


def sql(statement: str, tenant: str = TENANT) -> str:
    """Run SQL as the runtime's own login, in one tenant, or in none when tenant is empty."""
    setting = f"SELECT set_config('windops.tenant_id', '{tenant}', false); " if tenant else ""
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={setting}{statement}",
        namespace=DATA,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def _run_id(run_id: str) -> str:
    """Checked, so it is safe in the SQL below."""
    if not RUN_ID.fullmatch(run_id):
        raise RuntimeError(f"Not a run's ID: {run_id!r}")
    return run_id


def state(run_id: str) -> dict[str, Any]:
    run_id = _run_id(run_id)
    row = sql(
        "SELECT json_build_object('status', status, 'attempt', attempt, 'owner', lease_owner, "  # noqa: S608  # The run ID is checked.
        "'reason', reason, 'steps', (SELECT count(*) FROM run_steps s WHERE s.tenant_id = "
        f"r.tenant_id AND s.run_id = r.run_id)) FROM runs r WHERE run_id = '{run_id}'"
    )
    return dict(json.loads(row))


def steps(run_id: str) -> list[tuple[int, int, str]]:
    run_id = _run_id(run_id)
    rows = sql(
        "SELECT coalesce(json_agg(json_build_array(position, attempt, worker) ORDER BY position), "  # noqa: S608  # The run ID is checked.
        f"'[]') FROM run_steps WHERE run_id = '{run_id}'"
    )
    return [(p, a, w) for p, a, w in json.loads(rows)]


def wait_for(what: str, test: Callable[[], Any], timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = test()
        if value:
            return value
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {what}")


def pod_of(owner: str) -> str:
    """A worker's name is its pod's, then its slot."""
    return owner.rsplit("/", 1)[0]


def pods() -> list[str]:
    output = kubectl(
        "get", "pods", "--selector=app.kubernetes.io/name=windops-agent-runtime",
        "--field-selector=status.phase=Running", "--output=jsonpath={.items[*].metadata.name}",
        namespace=APPLICATION,
    ).stdout  # fmt: skip
    return output.split()


def deployment(*arguments: str) -> None:
    result = kubectl(*arguments, namespace=APPLICATION)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr.strip()}")


def settle(replicas: int) -> None:
    deployment("rollout", "status", f"deployment/{DEPLOYMENT}", "--timeout=180s")
    wait_for(f"{replicas} ready replicas", lambda: len(pods()) == replicas, timeout=120)


def docker(*arguments: str) -> str:
    """Docker on the Minikube node, where the containers run."""
    result = subprocess.run(
        ["minikube", "-p", KUBE_CONTEXT, "ssh", "--", "docker", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"docker {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def container_of(pod: str) -> str:
    found = docker(
        "ps", "--quiet", f"--filter=label=io.kubernetes.pod.name={pod}",
        f"--filter=label=io.kubernetes.container.name={CONTAINER}",
    )  # fmt: skip
    if not found or len(found.split()) != 1:
        raise RuntimeError(f"Cannot find the runtime's container in {pod}")
    return found


@contextmanager
def runtime(bundle: ClientBundle, key: str, pod: str | None = None) -> Iterator[Caller]:
    target = f"pod/{pod}" if pod else f"deployment/{DEPLOYMENT}"
    with (
        port_forward(target, APPLICATION, 50051) as port,
        blocking_channel(port, HOST, bundle) as channel,
    ):
        yield Caller(agent_runtime_pb2_grpc.AgentRuntimeServiceStub(channel), key)


def start(api: Caller, key: str, **parameters: str) -> pb.Run:
    request = pb.StartRunRequest(
        case_id=CASE, snapshot_id=SNAPSHOT, workflow="rehearsal", parameters=parameters,
        requested_by="user:runtime-check-engineer", idempotency_key=f"runtime-check-{RUN}-{key}",
    )  # fmt: skip
    run: pb.Run = api.call("StartRun", request, INVESTIGATION, TENANT, level=None).run
    return run


def investigation_bundle() -> ClientBundle:
    """The Investigation Service's own certificate, as it would call."""
    data = json.loads(
        kubectl(
            "get", "secret", "windops-investigation-tls", "--output=json", namespace=APPLICATION
        ).stdout
    )["data"]  # fmt: skip
    return ClientBundle(
        base64.b64decode(data["tls.crt"]),
        base64.b64decode(data["tls.key"]),
        base64.b64decode(data["ca.crt"]),
    )


def check_callers(key: str) -> None:
    with runtime(client_bundle(), key) as stranger:
        refused = [
            stranger.code(method, request, INVESTIGATION, TENANT, level=None)
            for method, request in (
                ("StartRun", pb.StartRunRequest(case_id=CASE)),
                ("GetRun", pb.GetRunRequest(run_id="run_" + "0" * 20)),
            )
        ]
    check(
        refused == [grpc.StatusCode.PERMISSION_DENIED] * 2,
        "Any certificate but the Investigation Service's is refused, even with a valid identity",
    )


def check_queue(investigation: ClientBundle, key: str) -> list[str]:
    with runtime(investigation, key) as api:
        began = time.monotonic()
        first = start(api, "queue")
        answered = time.monotonic() - began
        again = start(api, "queue")
        check(
            first.status == "queued" and answered < 2 and again.run_id == first.run_id,
            f"A run is queued at once, in {answered:.2f} s, and its key returns the same run",
        )
        others = [start(api, f"spread-{n}", steps="3", step_seconds="2") for n in range(4)]
        hidden = api.code(
            "GetRun", pb.GetRunRequest(run_id=first.run_id), INVESTIGATION, OTHER, level=None
        )
    runs = [first.run_id, *(run.run_id for run in others)]
    wait_for("the runs to complete", lambda: all(state(r)["status"] == "completed" for r in runs))
    workers = {pod_of(worker) for run in others for _, _, worker in steps(run.run_id)}
    check(
        len(workers) == 2,
        f"Both replicas took runs from the one queue: {len(runs)} completed, by {sorted(workers)}",
    )
    check(
        all(len(steps(run.run_id)) == 3 for run in others)
        and all(len({w for _, _, w in steps(run.run_id)}) == 1 for run in others),
        "Each run was held by one worker, and each step was done once",
    )
    check(hidden == grpc.StatusCode.NOT_FOUND, "Another tenant never sees the run")
    return runs


def check_takeover(investigation: ClientBundle, key: str) -> str:
    with runtime(investigation, key) as api:
        run = start(api, "takeover", steps="8", step_seconds="2")
    first = wait_for("two completed steps", lambda: (s := state(run.run_id))["steps"] >= 2 and s)
    stalled = pod_of(first["owner"])
    container = container_of(stalled)
    docker("pause", container)
    try:
        frozen_at = state(run.run_id)["steps"]
        taken = wait_for(
            "another replica to take the run",
            lambda: (s := state(run.run_id))["attempt"] == 2 and s["owner"] and s,
            timeout=LEASE_SECONDS * 3,
        )
    finally:
        docker("unpause", container)
    check(
        pod_of(taken["owner"]) != stalled,
        f"A stalled worker's run is taken over by the other replica once its {LEASE_SECONDS} s "
        f"lease runs out, as attempt 2",
    )
    wait_for("the run to complete", lambda: state(run.run_id)["status"] == "completed")
    done = steps(run.run_id)
    by_first = [p for p, attempt, _ in done if attempt == 1]
    check(
        [p for p, _, _ in done] == list(range(1, 9))
        and by_first == list(range(1, len(by_first) + 1))
        and len(by_first) >= frozen_at
        and all(pod_of(w) != stalled for _, attempt, w in done if attempt == 2),
        f"It carried on after its last completed step: steps 1 to {len(by_first)} by the first "
        f"worker, the rest by the second, and none done twice",
    )
    logs = wait_for(
        "the stalled worker to notice",
        lambda: "Lost a run's lease" in (
            out := kubectl("logs", f"pod/{stalled}", "--since=5m", namespace=APPLICATION).stdout
        ) and out,
        timeout=LEASE_SECONDS * 2,
    )  # fmt: skip
    check(
        bool(logs) and len(steps(run.run_id)) == 8,
        "Unfrozen, the stalled worker finds its lease gone, stops, and changes nothing",
    )
    return run.run_id


def check_handback(investigation: ClientBundle, key: str) -> str:
    with runtime(investigation, key) as api:
        run = start(api, "handback", steps="6", step_seconds="2")
    first = wait_for("a completed step", lambda: (s := state(run.run_id))["steps"] >= 1 and s)
    leaving = pod_of(first["owner"])
    deployment("delete", "pod", leaving, "--wait=false")
    taken = wait_for(
        "another replica to take the run",
        lambda: (s := state(run.run_id))["owner"] and pod_of(s["owner"]) != leaving and s,
        timeout=LEASE_SECONDS * 3,
    )
    check(
        taken["attempt"] == 1,
        "A worker shut down hands its run back at once: another replica took it, still attempt 1",
    )
    wait_for("the run to complete", lambda: state(run.run_id)["status"] == "completed")
    settle(2)
    return run.run_id


def check_cancel_and_attempts(investigation: ClientBundle, key: str) -> list[str]:
    with runtime(investigation, key) as api:
        running = start(api, "cancel", steps="20", step_seconds="2")
        failing = start(api, "attempts", steps="2", step_seconds="0", fail_at_step="1")
        wait_for("the run to start", lambda: state(running.run_id)["status"] == "running")
        asked = api.call(
            "CancelRun",
            pb.CancelRunRequest(run_id=running.run_id, requested_by="user:runtime-check-engineer",
                                reason="The check no longer needs it"),
            INVESTIGATION, TENANT, level=None,
        ).run  # fmt: skip
    ended = wait_for(
        "the run to be cancelled",
        lambda: (s := state(running.run_id))["status"] == "cancelled" and s,
        timeout=LEASE_SECONDS,
    )
    check(
        asked.cancel_requested and ended["steps"] < 20,
        "A running run is cancelled at its worker's next heartbeat, "
        f"after {ended['steps']} of 20 steps",
    )
    failed = wait_for(
        "the failing run to stop", lambda: (s := state(failing.run_id))["status"] == "failed" and s
    )
    check(
        failed["attempt"] == 3 and failed["reason"] == "The rehearsal was asked to fail here",
        "A failing run is tried three times, then fails, with its reason kept",
    )
    return [running.run_id, failing.run_id]


def check_database(run_id: str) -> None:
    check(
        sql("SELECT count(*) FROM runs", tenant="") == "0",
        "PostgreSQL hides every run from a query that names no tenant",
    )
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", f"--username={DATABASE}",
        f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command=SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608  # The run ID is checked.
        f"UPDATE run_steps SET worker = 'someone' WHERE run_id = '{_run_id(run_id)}'",
        namespace=DATA,
    )  # fmt: skip
    check(
        result.returncode != 0 and "never changes" in result.stderr,
        "PostgreSQL refuses to change a completed step",
    )


def check_audit(key: str, run_ids: list[str]) -> None:
    bootstrap(TENANT, AUDITOR, "auditor")
    wanted = {(run_ids[0], "agent_run.queued"), (run_ids[0], "agent_run.completed"),
              (run_ids[-1], "agent_run.failed")}  # fmt: skip
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, client_bundle()) as channel,
    ):
        audit = Caller(audit_pb2_grpc.AuditServiceStub(channel), key)

        def found() -> bool:
            records = audit.call(
                "ListAuditRecords",
                audit_pb2.ListAuditRecordsRequest(page_size=500),
                AUDITOR,
                TENANT,
            ).records
            seen = {(r.resource_id, r.action) for r in records if r.producer == "agent-runtime"}
            return wanted <= seen

        wait_for("the audit records", found, timeout=60)
    check(True, "Queuing and each run's end reached the tamper-evident audit trail")


def main() -> int:
    key = signing_key()
    original = kubectl(
        "get", f"deployment/{DEPLOYMENT}", "--output=jsonpath={.spec.replicas}",
        namespace=APPLICATION,
    ).stdout.strip()  # fmt: skip
    if not original:
        print(
            "The Agent Runtime is not deployed: run make migrate and make services", file=sys.stderr
        )
        return 1
    try:
        deployment(
            "set", "env", f"deployment/{DEPLOYMENT}", f"--containers={CONTAINER}",
            f"WINDOPS_RUNTIME_LEASE_SECONDS={LEASE_SECONDS}",
        )  # fmt: skip
        deployment("scale", f"deployment/{DEPLOYMENT}", "--replicas=2")
        settle(2)
        check(True, f"Two runtime replicas, with a {LEASE_SECONDS} s lease for the check")
        investigation = investigation_bundle()
        check_callers(key)
        queued = check_queue(investigation, key)
        taken = check_takeover(investigation, key)
        check_handback(investigation, key)
        ended = check_cancel_and_attempts(investigation, key)
        check_database(taken)
        check_audit(key, [queued[0], *ended])
    except (RuntimeError, grpc.RpcError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        kubectl(
            "set", "env", f"deployment/{DEPLOYMENT}", f"--containers={CONTAINER}",
            "WINDOPS_RUNTIME_LEASE_SECONDS-", namespace=APPLICATION,
        )  # fmt: skip
        kubectl(
            "scale", f"deployment/{DEPLOYMENT}", f"--replicas={original}", namespace=APPLICATION
        )
    print("Runtime check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
