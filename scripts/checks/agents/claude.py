#!/usr/bin/env python3
"""Prove the Claude policy on the cluster, with real calls to Claude through the runtime (M8.3).

Run `make secrets` with ANTHROPIC_API_KEY in .env, then `make migrate` and the services. The
check queues `model-check` runs, which send only synthetic text, a few cents' worth. It proves:

- only the runtime's pod mounts the Anthropic key, and LangSmith tracing is off until M8.9;
- a run pins the policy's version and hash, and its tenant's budget (AGT-004, LLM-010);
- each role's call reached Claude with the policy's model and effort (LLM-001, LLM-003), Opus 5.5
  with refusal fallback on (LLM-006), and stopped as it should (LLM-005);
- a structured answer came back through structured outputs (LLM-004);
- the stable prompt was read back from the cache (LLM-007);
- each call's cost matches its tokens at the policy's prices, and the run's usage adds them up;
- a run given less budget than one call costs ends as budget_exhausted after that call, its end
  reaches the audit trail, and WindopsAgentBudgetExhausted alerts a person (LLM-010).
"""

import json
import sys
import time
from decimal import Decimal

import grpc
import httpx
from windops.agent_runtime.v1 import agent_runtime_pb2 as pb
from windops_claude import Usage, cost_usd, load

from scripts.checks.agents.runtime import (
    APPLICATION,
    DEPLOYMENT,
    INVESTIGATION,
    RUN,
    TENANT,
    check_audit,
    investigation_bundle,
    runtime,
    sql,
    state,
    wait_for,
)
from scripts.checks.fleet.asset import check, kubectl, signing_key
from scripts.setup.gateway_routes import port_forward

OBSERVABILITY = "windops-observability"
KEY_SECRET = "windops-anthropic-api-key"  # noqa: S105  # A Secret's name, not a key.
ALERT = "WindopsAgentBudgetExhausted"
POLICY = load()


def check_key_and_tracing() -> None:
    deployments = json.loads(
        kubectl("get", "deployments", "--all-namespaces", "--output=json").stdout
    )["items"]
    mounting = sorted(
        d["metadata"]["name"]
        for d in deployments
        if any(
            (v.get("secret") or {}).get("secretName") == KEY_SECRET
            for v in d["spec"]["template"]["spec"].get("volumes", [])
        )
    )
    present = kubectl("get", "secret", KEY_SECRET, namespace=APPLICATION).returncode == 0
    check(
        present and mounting == [DEPLOYMENT],
        "Only the runtime's pod mounts the Anthropic key",
    )
    tracing = kubectl(
        "get", "configmap", "windops-agent-runtime-config",
        "--output=jsonpath={.data.LANGSMITH_TRACING}", namespace=APPLICATION,
    ).stdout  # fmt: skip
    check(tracing == "false", "LangSmith tracing stays off until traces are redacted (M8.9)")


def start(key: str, **budget: object) -> pb.Run:
    with runtime(investigation_bundle(), signing_key()) as api:
        request = pb.StartRunRequest(
            case_id="case_" + "e" * 20, snapshot_id="evs_" + "f" * 20, workflow="model-check",
            requested_by="user:claude-check-engineer",
            idempotency_key=f"claude-check-{RUN}-{key}", **budget,  # type: ignore[arg-type]
        )  # fmt: skip
        run: pb.Run = api.call("StartRun", request, INVESTIGATION, TENANT, level=None).run
        return run


def fetch(run_id: str) -> pb.Run:
    with runtime(investigation_bundle(), signing_key()) as api:
        run: pb.Run = api.call(
            "GetRun", pb.GetRunRequest(run_id=run_id), INVESTIGATION, TENANT, level=None
        ).run
        return run


def calls(run_id: str) -> list[dict[str, object]]:
    if not run_id.startswith("run_") or not run_id[4:].isalnum():
        raise RuntimeError(f"Not a run's ID: {run_id!r}")
    rows = sql(
        "SELECT coalesce(json_agg(row_to_json(c) ORDER BY position), '[]') FROM run_calls c "  # noqa: S608  # The run ID is checked.
        f"WHERE run_id = '{run_id}'"
    )
    return list(json.loads(rows))


def check_policy_run() -> str:
    run = start("policy")
    check(
        (run.policy_version, run.policy_hash) == (POLICY.version, POLICY.content_hash)
        and run.usage.token_budget == POLICY.budget_for(TENANT).tokens,
        f"A run pins the Claude policy, version {POLICY.version}, and its tenant's budget",
    )
    ended = wait_for(
        "the model check to end",
        lambda: (s := state(run.run_id))["status"] not in ("queued", "running") and s,
        timeout=600,
    )
    if ended["status"] != "completed":
        raise RuntimeError(f"The model check ended as {ended['status']}: {ended['reason']}")
    made = calls(run.run_id)
    roles = list(POLICY.roles)
    by_role = {c["role"]: c for c in made[: len(roles)]}
    check(
        all(
            by_role[name]["requested_model"] == role.model
            and by_role[name]["effort"] == role.effort
            for name, role in POLICY.roles.items()
        ),
        f"Each of the {len(roles)} roles called Claude with its policy's model and effort",
    )
    fell_back = [c for c in made if c["fallback"]]
    check(
        all(c["stop_reason"] == "end_turn" for c in made)
        and all(c["served_model"] == c["requested_model"] for c in made if not c["fallback"]),
        f"Every call stopped at end_turn, after its stop reason was checked; "
        f"{len(fell_back)} fell back",
    )
    check(
        len(made) == len(roles) + 2,
        "A structured answer came back through structured outputs, never forced tool choice",
    )
    last = made[-1]
    check(
        int(str(last["cache_read_tokens"])) > 0,
        f"The stable prompt was read from the cache: {last['cache_read_tokens']} tokens",
    )
    for c in made:
        usage = Usage(
            input_tokens=int(str(c["input_tokens"])),
            cache_write_5m_tokens=int(str(c["cache_write_5m_tokens"])),
            cache_write_1h_tokens=int(str(c["cache_write_1h_tokens"])),
            cache_read_tokens=int(str(c["cache_read_tokens"])),
            output_tokens=int(str(c["output_tokens"])),
        )
        if cost_usd(POLICY.pricing, str(c["served_model"]), usage) != Decimal(str(c["cost_usd"])):
            raise RuntimeError(f"Call {c['position']} was charged wrongly")
    total = sum(Decimal(str(c["cost_usd"])) for c in made)
    used = fetch(run.run_id).usage
    check(
        Decimal(used.cost_usd) == total and used.calls == len(made),
        f"Each call's cost matches its tokens at the policy's prices: ${total:.4f} for "
        f"{len(made)} calls, {used.tokens} tokens, {used.cache_read_tokens} read from the cache",
    )
    return run.run_id


def check_budget() -> str:
    # Less than one call costs, even when its prompt is read from the cache.
    run = start("budget", cost_budget_usd="0.001")
    ended = wait_for(
        "the small-budget run to end",
        lambda: (s := state(run.run_id))["status"] not in ("queued", "running") and s,
        timeout=300,
    )
    made = calls(run.run_id)
    check(
        ended["status"] == "budget_exhausted" and len(made) == 1,
        f"A run given $0.001 ends as budget_exhausted after one call: {ended['reason']}",
    )
    return run.run_id


def check_alert() -> None:
    with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as port:
        base = f"http://127.0.0.1:{port}"
        groups = httpx.get(f"{base}/api/v1/rules", timeout=20).json()["data"]["groups"]
        loaded = {rule["name"] for group in groups for rule in group["rules"]}
        check(ALERT in loaded, f"Prometheus has {ALERT}")
        firing = False
        for _ in range(30):
            alerts = httpx.get(f"{base}/api/v1/alerts", timeout=20).json()["data"]["alerts"]
            firing = any(a["labels"]["alertname"] == ALERT for a in alerts)
            if firing:
                break
            time.sleep(10)
    check(firing, "The run's end raised WindopsAgentBudgetExhausted, which alerts a person")


def main() -> int:
    try:
        check_key_and_tracing()
        completed = check_policy_run()
        exhausted = check_budget()
        check_audit(
            signing_key(),
            {(completed, "agent_run.completed"), (exhausted, "agent_run.budget_exhausted")},
            "Both runs' ends, budget_exhausted included, reached the tamper-evident audit trail",
        )
        check_alert()
    except (RuntimeError, grpc.RpcError, httpx.HTTPError) as error:
        print(error, file=sys.stderr)
        return 1
    print("Claude check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
