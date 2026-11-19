#!/usr/bin/env python3
"""Prove agent traces reach LangSmith complete, and only redacted (M8.9, EGR-003, OBS-005 to 008).

A free rehearsal of the team runs with tracing on, on a case whose title carries a planted e-mail
address and a planted API key. The Production Impact specialist's real get_case call returns
that title, so it flows through a real tool result into the trace. The check then reads the whole
trace back from LangSmith's own API, and proves:

- nothing planted, and no token, reached LangSmith (EGR-003, OBS-003);
- the trace's root names the run, case, snapshot, tenant, user and correlation IDs (OBS-005);
- the trace holds the graph's steps, each agent's model run with its model, effort and prompt
  version, and each tool call (OBS-005, OBS-006);
- every MCP tool call's audit record carries the run's ID as its correlation ID, as the trace
  does, so the two join (OBS-008).

Run `make secrets` with LANGSMITH_API_KEY in .env first. Nothing is asked of Claude.
"""

import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

import certifi
import grpc
import httpx
import jwt
from langsmith import Client
from windops.agent_runtime.v1 import agent_runtime_pb2 as rt
from windops.identity.v1 import identity_pb2 as pb

from scripts.checks.agents.runtime import (
    INVESTIGATION,
    investigation_bundle,
    runtime,
    sql,
    wait_for,
)
from scripts.checks.agents.tools import TURBINE, Cases
from scripts.checks.identity.agent_identity import as_user, identity_as
from scripts.checks.identity.identity import Realm
from scripts.checks.identity.token_exchange import TENANT, check, signing_key
from scripts.lib import settings
from scripts.lib.settings import KUBE_CONTEXT
from scripts.setup.identity_seed import demo_email, keycloak_public, read_demo_passwords

RUN = str(int(time.time()))
# Built from parts, so no scanner mistakes them for real secrets.
PLANTED_EMAIL = f"trace-check-{RUN}" + "@" + "north-sea.example"
PLANTED_KEY = "sk-" + "live-" + f"tracecheck{RUN}abcdefgh"
JWT_HEADER = "eyJhbGci"
STEPS = (
    "receive",
    "validate",
    "snapshot",
    "plan",
    "specialists",
    "documents",
    "research",
    "impact",
    "aggregate",
    "validate_output",
    "policy_checks",
    "recommend",
    "conclude",
)
AGENTS = {"supervisor", "scada-analyst", "reliability-engineer", "production-impact",
          "maintenance-planner", "safety-researcher"}  # fmt: skip


def langsmith() -> tuple[Client, str]:
    values = settings.load()
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    project = values.get("WINDOPS_LANGSMITH_PROJECT", "windops-ai")
    return Client(api_key=values["LANGSMITH_API_KEY"]), project


def start(
    realm: Realm, key: str, cases: Cases, *, title: str = "", sandbox: bool = False
) -> tuple[str, str, str]:
    """A traced rehearsal on a new case. With sandbox, the Reliability Engineer also runs a real
    Sandbox task (M8.10)."""
    login = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if login is None:
        raise RuntimeError("The demo operator could not sign in")
    user = str(jwt.decode(login.access, options={"verify_signature": False})["sub"])
    case_id = cases.open(
        user, title=title or f"Trace check: contact {PLANTED_EMAIL}, key {PLANTED_KEY}"
    )
    snapshot = cases.evidence(case_id)
    with identity_as(investigation_bundle()) as identity:
        delegation = identity.StartDelegation(
            pb.StartDelegationRequest(case_id=case_id, subject_token=login.access,
                                      idempotency_key=f"trace-check-{RUN}-{case_id[-6:]}"),
            **as_user(user, key),
        ).delegation.delegation_id  # fmt: skip
    with runtime(investigation_bundle(), key) as api:
        run = api.call("StartRun", rt.StartRunRequest(
            case_id=case_id, snapshot_id=snapshot, workflow="investigation",
            parameters={"team": "true", "rehearse": "true", "turbine_id": TURBINE,
                        **({"sandbox": "true"} if sandbox else {})},
            requested_by=f"user:{user}", idempotency_key=f"trace-check-{RUN}-{case_id[-6:]}",
            delegation_id=delegation,
        ), INVESTIGATION, TENANT, level=None).run  # fmt: skip
    wait_for(
        "the traced rehearsal to finish",
        lambda: (
            sql(f"SELECT status FROM runs WHERE run_id = '{run.run_id}'", tenant=TENANT)  # noqa: S608  # The runtime's own ID.
            == "completed"
        ),
        timeout=600,
    )
    return run.run_id, case_id, user


def read_trace(client: Client, project: str, run_id: str) -> tuple[Any, list[Any]]:
    """The trace's root, found by its run ID, and every run in it, once LangSmith has them."""
    wanted = f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))'
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        roots = list(client.list_runs(project_name=project, filter=wanted, is_root=True))
        if roots:
            runs = list(client.list_runs(project_name=project, trace_id=roots[0].trace_id))
            names = {r.name for r in runs}
            if {"recommend", "conclude"} <= names:
                return roots[0], runs
        time.sleep(10)
    raise RuntimeError("The trace never reached LangSmith in full")


def prove(client: Client, project: str, realm: Realm, key: str, cases: Cases) -> None:
    started = datetime.now(UTC)
    run_id, case_id, user = start(realm, key, cases)
    root, runs = read_trace(client, project, run_id)
    exported = json.dumps(
        [{"inputs": r.inputs, "outputs": r.outputs, "extra": r.extra, "error": r.error}
         for r in runs], default=str,
    )  # fmt: skip
    check(
        PLANTED_EMAIL not in exported and PLANTED_KEY not in exported
        and JWT_HEADER not in exported and "[REDACTED]" in exported,
        f"Of {len(runs)} runs exported, none holds the planted e-mail or key, or any token: "
        "the redaction ran before export (EGR-003)",
    )  # fmt: skip
    metadata = root.extra["metadata"]
    check(
        (metadata["run_id"], metadata["case_id"]) == (run_id, case_id)
        and metadata["tenant_id"] == TENANT
        and metadata["correlation_id"] == run_id
        and metadata["requested_by"] == f"user:{user}"
        and metadata["snapshot_id"].startswith("evs_"),
        "The trace's root names the run, case, snapshot, tenant, user and correlation IDs "
        "(OBS-005)",
    )  # fmt: skip
    names = {r.name for r in runs}
    steps = set(STEPS)
    models = [r for r in runs if r.run_type == "llm"]
    seen_agents = {(r.extra.get("metadata") or {}).get("agent") for r in models}
    check(
        steps <= names and seen_agents >= AGENTS
        and all({"model", "effort", "prompt_version"} <= set(r.extra["metadata"]) for r in models),
        f"The trace holds all 13 steps, {len(models)} model runs naming their agent, model, "
        "effort and prompt version, and every agent (OBS-006)",
    )  # fmt: skip
    tools = {r.name for r in runs if r.run_type == "tool"}
    check(
        {"windops_get_case", "windops_get_evidence", "weather_get_forecast", "task"} <= tools,
        f"It holds every tool call and handoff: {sorted(tools)}",
    )
    audited = audit_count(run_id, started)
    check(
        audited >= 6,
        f"{audited} MCP tool calls' audit records carry the run's ID as their correlation ID, as "
        "its trace does (OBS-008)",
    )


def audit_count(run_id: str, since: datetime) -> int:
    statement = (
        "SELECT set_config('windops.all_tenants', 'on', false); SELECT count(*) FROM "  # noqa: S608  # The runtime's own ID, and a clock reading.
        f"audit_records WHERE action LIKE 'mcp.%' AND correlation_id = '{run_id}' "
        f"AND recorded_at >= '{since.isoformat()}'"
    )
    deadline = time.monotonic() + 60
    found = 0
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-data", "exec",
             "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
             "--no-align", "--username=windops_audit", "--dbname=windops_audit",
             "--set=ON_ERROR_STOP=1", f"--command={statement}"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        found = int(result.stdout.strip().splitlines()[-1])
        if found >= 6:
            break
        time.sleep(3)
    return found


def main() -> int:
    try:
        key = signing_key()
        client, project = langsmith()
        with ExitStack() as stack:
            public = stack.enter_context(keycloak_public())
            cases = Cases(key)
            stack.callback(cases.close)
            prove(client, project, Realm(public), key, cases)
    except (
        RuntimeError,
        OSError,
        KeyError,
        grpc.RpcError,
        httpx.HTTPError,
        jwt.PyJWTError,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Trace check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
