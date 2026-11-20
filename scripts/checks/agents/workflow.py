#!/usr/bin/env python3
"""Prove the investigation workflow on the cluster: a worker killed mid-run, and the run resumed
from its last checkpoint (M8.4).

Run `make migrate` and the services first, with ANTHROPIC_API_KEY set: the Supervisor's plan is
one real call to Claude, a fraction of a cent. The check runs the runtime as two replicas with a
10-second lease, and restores both afterwards. It proves:

- a run goes through SRS 5.4's steps 1 to 13, a LangGraph checkpoint after each (FR-INV-007);
- a worker killed with SIGKILL after the Supervisor's plan loses at most the node it was in. The
  run is taken over once the lease runs out and carries on from the newest checkpoint: no step
  done twice, and the call to Claude never made again;
- a paused run stops at its worker's next heartbeat, does nothing while paused, and resumed,
  carries on from its checkpoint without spending an attempt;
- checkpoints never change, and PostgreSQL hides them from a query that names no tenant.
"""

import sys
import time

import grpc
from windops.agent_runtime.v1 import agent_runtime_pb2 as pb

from scripts.checks.agents.runtime import (
    APPLICATION,
    CONTAINER,
    DATA,
    DATABASE,
    DEPLOYMENT,
    INVESTIGATION,
    LEASE_SECONDS,
    RUN,
    TENANT,
    _run_id,
    container_of,
    deployment,
    docker,
    investigation_bundle,
    pod_of,
    runtime,
    settle,
    sql,
    state,
    steps,
    wait_for,
)
from scripts.checks.fleet.asset import check, kubectl, signing_key

STEPS = 13
CASE = "case_" + "g" * 20
SNAPSHOT = "evs_" + "h" * 20


def start(key: str, **parameters: str) -> pb.Run:
    with runtime(investigation_bundle(), signing_key()) as api:
        request = pb.StartRunRequest(
            case_id=CASE, snapshot_id=SNAPSHOT, workflow="investigation",
            parameters={"team": "false", **parameters},
            requested_by="user:workflow-check-engineer",
            idempotency_key=f"workflow-check-{RUN}-{key}",
        )  # fmt: skip
        run: pb.Run = api.call("StartRun", request, INVESTIGATION, TENANT, level=None).run
        return run


def call(method: str, request: object) -> pb.Run:
    with runtime(investigation_bundle(), signing_key()) as api:
        run: pb.Run = api.call(method, request, INVESTIGATION, TENANT, level=None).run
        return run


def count(table: str, run_id: str, where: str = "") -> int:
    return int(sql(f"SELECT count(*) FROM {table} WHERE run_id = '{_run_id(run_id)}' {where}"))  # noqa: S608  # Fixed table names and a checked ID.


def ended(run_id: str, timeout: float = 240) -> dict[str, object]:
    return dict(
        wait_for(
            "the run to end",
            lambda: (s := state(run_id))["status"] not in ("queued", "running", "paused") and s,
            timeout=timeout,
        )
    )


def check_kill() -> None:
    run = start("kill", step_seconds="2", plan_with_claude="true")
    check(
        run.status == "queued" and '"graph":"investigation@4"' in run.pins_json,
        "An investigation run is queued, pinned to its graph and its prompts (AGT-004)",
    )
    before = wait_for(
        "the Supervisor's plan and two steps after it",
        lambda: (s := state(run.run_id))["steps"] >= 6 and s,
        timeout=120,
    )
    victim = pod_of(str(before["owner"]))
    docker("kill", "--signal=KILL", container_of(victim))
    killed_at = count("run_steps", run.run_id)
    finished = ended(run.run_id)
    if finished["status"] != "completed":
        raise RuntimeError(f"The run ended as {finished['status']}: {finished['reason']}")
    done = steps(run.run_id)
    check(
        [p for p, _, _ in done] == list(range(1, STEPS + 1)) and finished["attempt"] == 2,
        f"Killed with SIGKILL after step {killed_at}, the run was taken over once its "
        f"{LEASE_SECONDS} s lease ran out, and completed all {STEPS} steps as attempt 2, "
        "none done twice",
    )
    first = [p for p, attempt, _ in done if attempt == 1]
    check(
        first == list(range(1, len(first) + 1)) and len(first) >= 6,
        f"It carried on from its newest checkpoint: steps 1 to {len(first)} before the kill, "
        "the rest after",
    )
    joined = sql(
        "SELECT count(*) FROM run_checkpoints c JOIN run_checkpoints p ON p.run_id = c.run_id "  # noqa: S608  # A checked ID.
        "AND p.checkpoint_id = c.parent_id WHERE c.attempt = 2 AND p.attempt = 1 "
        f"AND c.run_id = '{_run_id(run.run_id)}'"
    )
    check(
        int(joined) == 1 and count("run_checkpoints", run.run_id) >= STEPS,
        f"The resumed run's first checkpoint follows the killed worker's last, of "
        f"{count('run_checkpoints', run.run_id)} in all",
    )
    check(
        count("run_calls", run.run_id) == 1,
        "The Supervisor's plan was asked of Claude once: the resumed run read it from its "
        "checkpoint",
    )
    settle(2)


def check_pause() -> None:
    run = start("pause", step_seconds="2")
    wait_for("two steps", lambda: state(run.run_id)["steps"] >= 2, timeout=60)
    asked = call(
        "PauseRun",
        pb.PauseRunRequest(run_id=run.run_id, requested_by="user:workflow-check-engineer",
                           reason="Waiting on a site visit"),
    )  # fmt: skip
    paused = wait_for(
        "the run to pause",
        lambda: (s := state(run.run_id))["status"] == "paused" and s,
        timeout=LEASE_SECONDS * 2,
    )
    held = int(str(paused["steps"]))
    time.sleep(8)
    check(
        asked.pause_requested and state(run.run_id)["steps"] == held and held < STEPS,
        f"A paused run stops at its worker's next heartbeat, after {held} steps, and does "
        "nothing while paused",
    )
    call("ResumeRun", pb.ResumeRunRequest(run_id=run.run_id,
                                          requested_by="user:workflow-check-engineer"))  # fmt: skip
    finished = ended(run.run_id)
    done = steps(run.run_id)
    check(
        finished["status"] == "completed"
        and finished["attempt"] == 1
        and [p for p, _, _ in done] == list(range(1, STEPS + 1)),
        "Resumed, it carried on from its checkpoint to step 13, still attempt 1",
    )


def check_database(run_id: str) -> None:
    check(
        sql("SELECT count(*) FROM run_checkpoints", tenant="") == "0",
        "PostgreSQL hides every checkpoint from a query that names no tenant",
    )
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", f"--username={DATABASE}",
        f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command=SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608  # A checked ID.
        f"DELETE FROM run_checkpoints WHERE run_id = '{_run_id(run_id)}'",
        namespace=DATA,
    )  # fmt: skip
    check(
        result.returncode != 0 and "never changes" in result.stderr,
        "PostgreSQL refuses to change or remove a checkpoint",
    )


def main() -> int:
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
        check_kill()
        check_pause()
        last = sql(
            "SELECT run_id FROM runs WHERE workflow = 'investigation' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        check_database(last)
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
    print("Workflow check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
