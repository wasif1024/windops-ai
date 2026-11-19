#!/usr/bin/env python3
"""Rehearse the investigation team on the cluster: the real team, tools and tokens, no cost (M8.7).

The demo operator signs in and opens a case on the demo turbine, whose evidence the platform
freezes. The Investigation Service's certificate delegates to agents for them, and starts an
investigation run that rehearses its team: the Agent Runtime assembles the real Supervisor and
specialists, with their restrictions, skills, tools and tokens, and swaps only the model for a
script. No call reaches Claude, and no web search is made. The check proves:

- the run completes: the Supervisor delegates to every specialist, through Deep Agents' task tool;
- each agent called a real MCP tool from its own allowlist, with a token for that agent and tool
  alone, over mutual TLS from the runtime: WindOps, Documentation and Weather MCP answered;
- each call was audited as that agent, on the user's behalf (SEC-010);
- the result is structured as SRS 5.7 sets out, cites only what the tools returned, and is kept
  on the run, and its recommended action went through the real Policy Service (M8.8);
- the run is pinned to every agent's prompt, tool list and skill (AGT-004);
- no token was kept in the run's checkpoints (SEC-008), and nothing was asked of Claude.

The run with Claude itself is the M8.13 exit gate's. The case is cancelled at the end.
"""

import base64
import json
import re
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

import grpc
import httpx
import jwt
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from windops.agent_runtime.v1 import agent_runtime_pb2 as rt
from windops.identity.v1 import identity_pb2 as pb
from windops_identity.targets import AGENT_TOOLS

from scripts.checks.agents.runtime import (
    INVESTIGATION,
    investigation_bundle,
    runtime,
    sql,
    wait_for,
)
from scripts.checks.agents.tools import Cases
from scripts.checks.identity.agent_identity import as_user, audited, identity_as
from scripts.checks.identity.identity import Realm
from scripts.checks.identity.token_exchange import TENANT, check, signing_key
from scripts.setup.identity_seed import demo_email, keycloak_public, read_demo_passwords

RUN = str(int(time.time()))
TURBINE = "dogger-east-t01"
REPORT = "Rehearsal report: every specialist answered, and nothing was concluded."
EXPECTED = {
    "windops_get_evidence",
    "windops_get_turbine",
    "documentation_search_documents",
    "windops_get_case",
    "windops_list_cases",
    "weather_get_forecast",
}
AUDITED = {
    ("mcp.windops.get_evidence", "agent:supervisor"),
    ("mcp.windops.get_turbine", "agent:scada-analyst"),
    ("mcp.documentation.search_documents", "agent:reliability-engineer"),
    ("mcp.windops.get_case", "agent:production-impact"),
    ("mcp.windops.list_cases", "agent:maintenance-planner"),
    ("mcp.weather.get_forecast", "agent:safety-researcher"),
}
RUN_ID = re.compile(r"run_[a-z0-9]{20}")


def run_sql(statement: str, run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise RuntimeError(f"Not a run's ID: {run_id!r}")
    return sql(statement.replace(":run", f"'{run_id}'"), tenant=TENANT)


def final_state(run_id: str) -> dict[str, Any]:
    row = run_sql(
        "SELECT checkpoint_type || '|' || replace(encode(checkpoint, 'base64'), E'\n', '') "
        "FROM run_checkpoints WHERE run_id = :run ORDER BY checkpoint_id DESC LIMIT 1", run_id,
    )  # fmt: skip
    kind, _, data = row.partition("|")
    checkpoint = JsonPlusSerializer().loads_typed((kind, base64.b64decode(data.replace("\n", ""))))
    state: dict[str, Any] = checkpoint["channel_values"]
    return state


def prove(realm: Realm, key: str, cases: Cases) -> None:
    started = datetime.now(UTC)
    login = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if login is None:
        raise RuntimeError("The demo operator could not sign in")
    user = str(jwt.decode(login.access, options={"verify_signature": False})["sub"])
    case_id = cases.open(user)
    snapshot = cases.evidence(case_id)
    with identity_as(investigation_bundle()) as identity:
        delegation = identity.StartDelegation(
            pb.StartDelegationRequest(case_id=case_id, subject_token=login.access,
                                      idempotency_key=f"team-check-{RUN}"),
            **as_user(user, key),
        ).delegation.delegation_id  # fmt: skip
    with runtime(investigation_bundle(), key) as api:
        run = api.call("StartRun", rt.StartRunRequest(
            case_id=case_id, snapshot_id=snapshot, workflow="investigation",
            parameters={"team": "true", "rehearse": "true", "turbine_id": TURBINE},
            requested_by=f"user:{user}", idempotency_key=f"team-check-{RUN}",
            delegation_id=delegation,
        ), INVESTIGATION, TENANT, level=None).run  # fmt: skip
    pins = json.loads(run.pins_json)
    check(
        set(pins["prompts"]) >= set(AGENT_TOOLS)
        and set(pins["tools"]) == set(AGENT_TOOLS)
        and len(pins["skills"]) == 6,
        "The run is pinned to every agent's prompt and tool list, and all six skills (AGT-004)",
    )
    ended = wait_for(
        "the team to finish",
        lambda: (s := run_sql("SELECT status || '|' || reason FROM runs WHERE run_id = :run",
                              run.run_id)).split("|")[0] not in ("queued", "running") and s,
        timeout=900,
    )  # fmt: skip
    status, _, reason = str(ended).partition("|")
    if status != "completed":
        raise RuntimeError(f"The run ended as {status}: {reason}")
    state = final_state(run.run_id)
    specialists = set(AGENT_TOOLS) - {"supervisor"}
    check(
        state["report"] == REPORT and set(state["delegated"]) == specialists,
        f"The Supervisor delegated to all {len(specialists)} specialists through the task tool, "
        "and reported",
    )
    used = state["tools_used"]
    allowed = {
        s.removeprefix("mcp.").replace(".", "_") for tools in AGENT_TOOLS.values() for s in tools
    }
    check(
        set(used) == EXPECTED and set(used) <= allowed and not any("tavily" in t for t in used),
        f"Each agent called a real tool on its own allowlist, {len(used)} in all, and none "
        "searched the web",
    )
    kept = json.loads(run_sql("SELECT result_json FROM runs WHERE run_id = :run", run.run_id))
    fields = {"case_id", "asset_id", "snapshot_id", "observed_facts", "hypotheses",
              "evidence_references", "conflicting_evidence", "data_gaps", "risk_flags",
              "recommended_actions", "confidence", "explanation", "escalation_required",
              "policy_result_id", "model_versions", "generated_at"}  # fmt: skip
    check(
        fields <= set(kept) and kept["case_id"] == case_id and kept["snapshot_id"] == snapshot
        and all(c in state["seen"]["references"] for f in kept["observed_facts"]
                for c in f["citations"]),
        "The result has every SRS 5.7 field, and cites only references its tools returned "
        "(FR-INV-003, FR-INV-004)",
    )  # fmt: skip
    policy = kept["policy"]
    check(
        len(policy) == len(kept["recommended_actions"]) and kept["policy_result_id"]
        and kept["policy_result_id"] == policy[0]["result_id"]
        and kept["escalation_required"] is True,
        f"Each recommended action went through the Policy Service: {policy[0]['outcome']}, "
        f"{kept['policy_result_id']}, so a person must look",
    )  # fmt: skip
    calls = run_sql("SELECT count(*) FROM run_calls WHERE run_id = :run", run.run_id)
    check(calls == "0", "Nothing was asked of Claude: the rehearsal costs nothing")
    deadline, seen = time.monotonic() + 60, set()
    while time.monotonic() < deadline:
        seen = {
            (action, actor) for action, kind, actor, behalf in audited(started)
            if kind == "agent" and behalf == user
        }  # fmt: skip
        if seen >= AUDITED:
            break
        time.sleep(3)
    check(
        seen >= AUDITED,
        f"Every agent's call reached the Audit Service as that agent, on the user's behalf: "
        f"{len(AUDITED)} agents",
    )
    # A JWT's header starts "eyJhbGci": look for one in every checkpoint and write, as stored.
    header = "'\\x65794a6862476369'::bytea"
    holding = run_sql(
        "SELECT (SELECT count(*) FROM run_checkpoints WHERE run_id = :run "  # noqa: S608  # A constant, and a checked ID.
        f"AND position({header} IN checkpoint) > 0) + (SELECT count(*) FROM "
        f"run_checkpoint_writes WHERE run_id = :run AND position({header} IN value) > 0)",
        run.run_id,
    )
    check(
        holding == "0",
        "No token was kept in the run's checkpoints (SEC-008)",
    )


def main() -> int:
    try:
        key = signing_key()
        with ExitStack() as stack:
            public = stack.enter_context(keycloak_public())
            cases = Cases(key)
            stack.callback(cases.close)
            prove(Realm(public), key, cases)
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
    print("Team check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
