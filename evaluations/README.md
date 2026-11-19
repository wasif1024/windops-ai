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
make regression                          # the set on the cluster, rehearsed until M8.13, scores kept in LangSmith
make regression SCENARIO=gearbox-temp-001
```

A rehearsal judges only what the platform decides: the run completing, the result's shape, its citations, tool
permissions and prohibited actions. Diagnosis, escalation and the tools chosen are judged when Claude runs.
