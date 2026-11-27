#!/usr/bin/env python3
"""Run the live suite now, and show the latest runs (EVL-001, M12.6b).

The suite runs inside the Evaluation Service, daily, in the deployment's live tenant: a turbine's
forecast from the real weather provider, and research and a page from the real Tavily, each
through the platform's own services. This asks it to run once more, as a Platform Administrator,
and prints each check. It spends one Tavily search and one extraction, from the live tenant's
daily research allowance, and no Claude.

    make live-suite
"""

import secrets
import sys

import grpc
from windops.evaluation.v1 import evaluation_pb2 as pb

from scripts.evaluation.service import evaluations


def show(run: pb.LiveRun) -> None:
    print(f"{run.run_id} in {run.live_tenant_id}, by {run.triggered_by}: {run.outcome}")
    for check in run.checks:
        print(f"  {'ok' if check.passed else 'FAILED'} - {check.name}: {check.detail}")


def main() -> int:
    try:
        with evaluations() as api:
            ran = api.call("RunLiveSuite", pb.RunLiveSuiteRequest(
                idempotency_key=f"live-suite-{secrets.token_hex(8)}")).run  # fmt: skip
            show(ran)
            earlier = api.call("ListLiveRuns", pb.ListLiveRunsRequest(page_size=6)).runs
            others = [r for r in earlier if r.run_id != ran.run_id][:5]
            if others:
                print("Earlier runs:")
                for run in others:
                    print(f"  {run.run_id} ({run.triggered_by}): {run.outcome}")
    except (grpc.RpcError, RuntimeError, OSError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Live suite passed" if ran.outcome == "passed" else "Live suite failed")
    return 0 if ran.outcome == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
