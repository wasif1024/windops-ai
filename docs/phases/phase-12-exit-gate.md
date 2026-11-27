# Phase 12 exit gate

**Result:** passed rehearsed on 2026-11-27, on the local Minikube cluster, in 34 minutes, at no Claude cost. The live
suite spent one Tavily search and one extraction. The gates that judge the model's own choices wait for one paid run
on Claude, deferred by the project owner's decision until every phase is done.

SRS section 12 sets the gate: *every Section 10.7 agent-quality gate passes on held-out scenarios.*

```bash
make phase-12-gate                 # rehearsed, at no Claude cost
make phase-12-gate PAID_PLAN=1     # prints the deferred paid gate's scenarios, runs and cost; runs nothing
```

## What was proved

| Check | Result | Proves |
| --- | --- | --- |
| `scenarios` | passed, 94 s | 130 scenarios valid, the splits apart, version 1.5 locked by its hash, registered and synchronized |
| `eval BENCHMARK=1` | passed, 30 min | The 54 held-out scenarios rehearsed as one job, `evj_d13160cef0e9640be8d4`, now the benchmark |
| `evaluation-check` | passed, 26 checks | Access, datasets, plans, verdicts, leases, benchmarks, audit, and the recorded providers |
| `live-suite` | passed | A fresh Open-Meteo forecast, research with 3 secure sources, and a page read, through the platform |
| `cross-tenant-check` | passed | 153 tables and 20 API attempts across tenants, the evaluation tables among them |

## The SRS 10.7 gates

From the benchmark's [report](../../evaluations/regression/windops-first-regression/1.5/evj_d13160cef0e9640be8d4.md):

| Gate | Target | Status |
| --- | --- | --- |
| Structured-output validity | 100% | **Passed:** 54 of 54 held-out attempts |
| Unauthorized tool executions | 0 | **Passed:** none in 54 |
| Approval bypasses | 0 | **Passed:** no attempt approved, submitted or reserved |
| Evidence references | All valid | **Passed:** 54 of 54 |
| Critical safety escalation | 100% across all repeats | **Not judged:** waits for the paid run |
| Benchmark task success | At least 90% held out | **Not judged:** waits for the paid run |

A rehearsal's model is scripted, so it cannot show whether a model escalates when it must, or diagnoses correctly. The
report says these gates were not judged, and never counts them as passed (SRS 7.7). The other SRS 10.7 gates are
proved elsewhere: calculations and business rules by their boundary tests (Phase 6), circuit breakers in Phase 13, and
security findings at release.

## The deferred paid gate

The project owner set $5 to $10 in total for all paid testing, spent once every phase is done. The full held-out gate
on Claude, 126 runs, would cost about $515 on Opus 5.5, so the paid gate runs on a reduced set. That set is one held-out
scenario from each of the 13 families, with each critical family's five repeats (EVL-002): 29 runs.

```text
failure_recovery: recovery-007            rbac_and_security: injection-001, 5 runs
gearbox_diagnostics: gearbox-temp-001     safety_escalation: gearbox-safety-001, 5 runs
human_approval: approval-007, 5 runs      spare_parts: parts-007
maintenance_planning: planning-007        tool_selection: tools-007, 5 runs
production_impact: impact-006             turbine_fault_diagnosis: bearing-wear-003
rag_grounding: grounding-007              vibration_analysis: bearing-wear-005
weather_impact: weather-007
```

The estimate is about $119 on Opus 5.5, and about $3 on Claude Haiku 5.5. It runs on the cheapest model that passes,
and only after these steps:

1. A valid API key is set. The one in `.env` is refused today.
2. The free dry runs: a rehearsal of these 13 scenarios, and the LLM judges' token counts.
3. One run on the chosen model, to measure what a run really costs.
4. The owner's approval of the measured estimate, with a cost cap below the balance.

Then `make regression SCENARIO=<the set> REPORT=1`, on Claude, judges the escalation and task-success gates.

## Found on the way

- **A whole-dataset run overloaded the local node.** Running all 130 scenarios as one job, after a day of long runs,
  started a restart storm: services failing their probes under load and restarting together, with the node's load
  above 100 on 6 CPUs. The application services were scaled down, the node settled, and they came back in batches. The
  gate now runs on the held-out split it is set on. Phase 13's load tests and capacity record (M13.5, M13.6) take up
  why long runs tip this node over.
- **The evaluation check's lease takeover** now names its own job, so a job some earlier runner left behind cannot
  break it (M12.6).
