#!/usr/bin/env python3
"""Check the Phase 12 exit gate on the real local cluster.

SRS section 12: every Section 10.7 agent-quality gate passes on held-out scenarios. Run free, in
two parts, as the user decided on 2026-11-27 (a paid-testing budget of $5 to $10 in all, spent
once every phase is done):

- **Now, rehearsed, at no cost:** the held-out split, which the gate is set on, as one job, made
  the benchmark, with its report and gates. The whole set of 130 overloaded the local node in
  one run, and its other splits are rehearsed by each module's own runs. A rehearsal decides
  the platform's gates on every attempt: structured-output
  validity, unauthorized tools, approval bypasses and evidence references. It reports the gates
  on the model's own choices, critical safety escalation and held-out task success, as not
  judged, never as passed.
- **Deferred, on Claude:** those two gates, on a reduced held-out set, one scenario a family with
  each critical family's five repeats, on the cheapest model that passes. `--paid-plan` prints
  that set, its runs and its cost, without running anything.

Around the gate run the checks for what Phase 12 built: the scenario pipeline, the Evaluation
Service (datasets, jobs, leases, outcomes, benchmarks, recordings), the live suite, and isolation
across tenants. Every check runs, so one failure does not hide another. The live suite spends one
Tavily search and one extraction; nothing here spends Claude credit.

**The node:** it waits for a calm node before each check, and pauses the observability stack for
the run, restoring it at the end. Name checks to run only those:
`uv run python -m scripts.checks.gates.phase_12 evaluation-check`.
"""

import argparse
import sys
import tempfile
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from scripts.checks.gates.phase_10 import calm, run
from scripts.checks.gates.phase_11 import scale
from scripts.evaluation.scenarios import REPEATED, Scenario, dataset

# (make target and its settings, what it proves for the gate)
CHECKS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("scenarios",), "130 scenarios valid, the splits apart, the version locked and registered"),
    (("eval", "BENCHMARK=1"),
     "the held-out split rehearsed as one job, the benchmark, with every gate a rehearsal decides"),
    (("evaluation-check",), "datasets, jobs, leases, outcomes, benchmarks and recorded providers"),
    (("live-suite",), "the real Tavily and weather providers, through the platform"),
    (("cross-tenant-check",), "every database and API, the evaluation tables among them"),
)  # fmt: skip
# Measured: about $4.09 a run on Opus 5.5 in M8.13; Haiku 5.5 costs 1/40 of Opus 5.5 a token.
OPUS_RUN_USD = Decimal("4.09")
HAIKU_SHARE = Decimal("0.025")


def paid_set(scenarios: Sequence[Scenario]) -> list[Scenario]:
    """The deferred paid gate: one held-out scenario a family, the first by its ID."""
    chosen: dict[str, Scenario] = {}
    for s in sorted(scenarios, key=lambda s: s.case_id):
        if s.split == "held_out":
            chosen.setdefault(s.family, s)
    return sorted(chosen.values(), key=lambda s: (s.family, s.case_id))


def paid_plan() -> int:
    _, scenarios = dataset()
    chosen = paid_set(scenarios)
    runs = sum(s.runs_required for s in chosen)
    print("The deferred paid gate (SRS 10.7 on Claude), one held-out scenario a family:")
    for s in chosen:
        repeats = f", {s.runs_required} runs" if s.family in REPEATED else ""
        print(f"  {s.family}: {s.case_id}{repeats}")
    print(f"{len(chosen)} scenarios, {runs} runs. About ${runs * OPUS_RUN_USD:.0f} on Opus 5.5, "
          f"about ${runs * OPUS_RUN_USD * HAIKU_SHARE:.2f} on Haiku 5.5, before a run measures "
          "it. Nothing was run.")  # fmt: skip
    print("  make regression SCENARIO=" + ",".join(s.case_id for s in chosen) + " REPORT=1")
    return 0


def main(only: Sequence[str]) -> int:
    names = {target[0] for target, _ in CHECKS}
    unknown = set(only) - names
    if unknown:
        print(f"Not a gate check: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    chosen = [(t, proves) for t, proves in CHECKS if not only or t[0] in only]
    logs = Path(tempfile.mkdtemp(prefix="windops-phase-12-gate-"))
    print(f"Logs: {logs}", flush=True)
    failed: list[str] = []
    started = time.monotonic()
    scale(0)
    try:
        for target, proves in chosen:
            note = calm()
            passed, seconds, log = run(target, logs)
            if passed:
                print(f"ok - {target[0]}{note} ({seconds} s): {proves}", flush=True)
            else:
                print(f"not ok - {target[0]}{note} ({seconds} s): {proves}. See {log}",
                      flush=True)  # fmt: skip
                failed.append(target[0])
    finally:
        scale(1)
    minutes = int((time.monotonic() - started) // 60)
    if failed:
        print(f"Phase 12 exit gate failed in {minutes} minutes: {', '.join(failed)}",
              file=sys.stderr)  # fmt: skip
        return 1
    if only:
        print(f"The {len(chosen)} checks named passed in {minutes} minutes")
        return 0
    print(f"Phase 12 exit gate passed in {minutes} minutes, rehearsed: all {len(CHECKS)} checks. "
          "The gates on the model's choices wait for the paid run (--paid-plan)")  # fmt: skip
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checks", nargs="*", help="run only these")
    parser.add_argument("--paid-plan", action="store_true",
                        help="print the deferred paid gate; runs nothing")  # fmt: skip
    arguments = parser.parse_args()
    sys.exit(paid_plan() if arguments.paid_plan else main(arguments.checks))
