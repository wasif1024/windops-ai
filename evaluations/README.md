# Evaluations

Versioned scenarios for the agents, scored against the simulator's hidden ground truth (SRS 10.5 to 10.7,
[ADR 0026](../docs/adr/0026-the-first-regression-set-and-agent-dashboards.md)).

| Folder | Holds |
| --- | --- |
| `scenarios/` | One JSON file a scenario: its family, split, the simulator scenario replayed, the ground truth, the case a person opens, the tools a good investigation calls, what must never happen, and how many runs it needs |
| `datasets/` | Versioned lists of scenarios. `first-regression.json` is M8.12's twelve |
| `evaluators/`, `benchmarks/`, `recordings/`, `regression/` | Phase 12: LLM judges, benchmarks, recorded Tavily and weather answers (EVL-001), and regression history |

The deterministic evaluators and the runner are in [scripts/evaluation](../scripts/evaluation/). Agents never see this
folder: the ground truth stays here (SIM-005).

```bash
uv run pytest tests/test_evaluation.py   # every scenario valid, and each evaluator catching what it is for
make regression                          # the set on the cluster, as a job of the Evaluation Service
make regression SCENARIO=gearbox-temp-001
make regression BENCHMARK=1              # and make it the benchmark, if it passes
```

The [Evaluation Service](../services/evaluation-service/README.md) keeps each dataset version, each job and every
attempt, and gives the verdict: passed only when every planned attempt passed, never when one was skipped
([ADR 0042](../docs/adr/0042-the-evaluation-service-keeps-the-record-and-the-verdict.md)). Each job is compared with
its benchmark, so a regression is named.

A rehearsal judges only what the platform decides: the run completing, the result's shape, its citations, tool
permissions and prohibited actions. Diagnosis, escalation and the tools chosen are judged when Claude runs.
