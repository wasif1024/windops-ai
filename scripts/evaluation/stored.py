"""What a judge is given, read from the cluster: a run's result, and its evidence's content.

Every query names its tenant: the services' own logins read across tenants, so nothing here
relies on a tenant setting to keep another tenant's rows out.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from scripts.checks.fleet.maintenance import sql
from scripts.evaluation import pipeline
from scripts.evaluation.judges import Judge, count, material
from scripts.evaluation.scenarios import dataset
from scripts.lib import settings

# A judge's answer: a score, a few sentences and a few problems, with its thinking.
EXPECTED_OUTPUT_TOKENS = 1500


def _quoted(value: str) -> str:
    if not value.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"Not an ID: {value!r}")
    return f"'{value}'"


def evidence_of(tenant: str, snapshot_id: str) -> dict[str, str]:
    """Each evidence item's content, by its record ID, in the snapshot."""
    found = sql(
        "SELECT coalesce(json_object_agg(record_id, content), '{}') FROM evidence_items "  # noqa: S608  # IDs checked above.
        f"WHERE tenant_id = {_quoted(tenant)} AND snapshot_id = {_quoted(snapshot_id)}",
        "windops_investigation",
    ).splitlines()[-1]
    return {str(k): str(v) for k, v in json.loads(found).items()}


def run_of(run_id: str) -> tuple[str, Mapping[str, Any]]:
    """A run's tenant and its result."""
    found = sql(
        "SELECT json_build_object('tenant', tenant_id, 'result', result_json) FROM runs "  # noqa: S608
        f"WHERE run_id = {_quoted(run_id)}",
        "windops_agent_runtime",
    ).splitlines()[-1]
    row = json.loads(found)
    if not row.get("result"):
        raise ValueError(f"Run {run_id} has no result")
    return str(row["tenant"]), json.loads(row["result"])


def dry_run(judges: list[Judge], run_id: str, scenario_id: str, exact: bool = True) -> int:
    """Each judge's input for a stored run, and what judging it would cost on the evaluator
    role's model. Exact, from Anthropic's free token counting; or, with no key to count with, an
    estimate of 4 characters a token, said to be one. Nothing is sent to a model."""
    from windops_claude import Usage, cost_usd
    from windops_claude import load as load_policy

    from scripts.evaluation.evaluators import Observed
    from scripts.evaluation.judges import messages

    _, scenarios = dataset()
    scenario = next(s for s in scenarios if s.case_id == scenario_id)
    tenant, result = run_of(run_id)
    observed = Observed(scenario_id=scenario_id, run_id=run_id, run_status="completed",
                        case_status="pending_review", rehearsed=False, result=result)  # fmt: skip
    evidence = evidence_of(tenant, str(result.get("snapshot_id", "")))
    truth = pipeline.ground_truth(scenario)
    given = [material(j, scenario, observed, evidence, truth) for j in judges]
    policy = load_policy()
    model = policy.role("evaluator").model
    if exact:
        key = settings.load().get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("No ANTHROPIC_API_KEY to count tokens with: use --estimate")
        sizes, how = count(judges, given, model, key), "counted"
    else:
        sizes = [sum(len(str(m.content)) for m in messages(j, d)) // 4
                 for j, d in zip(judges, given, strict=True)]  # fmt: skip
        sizes, how = sizes, "estimated at 4 characters a token"
    total = Decimal(0)
    for judge, data, tokens in zip(judges, given, sizes, strict=True):
        usage = Usage(input_tokens=tokens, output_tokens=EXPECTED_OUTPUT_TOKENS)
        cost = cost_usd(policy.pricing, model, usage)
        total += cost
        cited = len(data["evidence"]) if "evidence" in data else "-"
        print(f"{judge.label}: {tokens} input tokens ({how}), evidence items {cited}, about "
              f"${cost:.4f} with {EXPECTED_OUTPUT_TOKENS} output tokens on {model}")  # fmt: skip
    print(f"Judging one run: about ${total:.4f}. Nothing was sent to a model.")
    return 0
