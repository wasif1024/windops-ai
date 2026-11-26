# Evaluations

Versioned scenarios for the agents, scored against the simulator's hidden ground truth (SRS 10.5 to 10.7,
[ADR 0026](../docs/adr/0026-the-first-regression-set-and-agent-dashboards.md)).

| Folder | Holds |
| --- | --- |
| `scenarios/` | One JSON file a scenario: its family, split, the simulator scenario replayed and its seed, the ground truth, the case a person opens, the tools a good investigation calls, what must never happen, and how many runs it needs |
| `datasets/` | Versioned lists of scenarios, each version locked by its content hash. `first-regression.json` is M8.12's twelve |
| `evaluators/`, `benchmarks/`, `recordings/`, `regression/` | Phase 12: LLM judges, benchmarks, recorded Tavily and weather answers (EVL-001), and regression history |

The deterministic evaluators and the runner are in [scripts/evaluation](../scripts/evaluation/). Agents never see this
folder: the ground truth stays here (SIM-005).

## The scenario pipeline (M12.2)

[ADR 0043](../docs/adr/0043-seeded-variants-split-replays-and-locked-dataset-versions.md):

- **Variants:** a scenario may replay a simulator scenario with a seed of its own: the same fault, at the same time, with
  its own noise and link trouble. That is how the set grows to 100 to 200 without new simulator code. A variant's seed
  must not be another simulator scenario's own.
- **Separate splits (EVL-006):** each replay, a simulator scenario with its seed, belongs to one split, and no two
  scenarios share a case title. No held-out telemetry is ever seen while prompts are tuned.
- **Ground truth (SIM-005):** the simulator's own record of each replay, for its seed, goes into the scenario's
  definition for the Evaluation Service and into LangSmith's outputs, never into an input.
- **Locked versions (EVL-005):** a dataset manifest keeps each version's content hash, the hash the Evaluation Service
  gives it. Changing any scenario fails the tests until the dataset has a new version, locked with
  `make scenarios-lock`; an old version's hash is never rewritten.
- **`make scenarios`** checks the set, registers the version with the Evaluation Service, and synchronizes it to
  LangSmith, each scenario an example in its split and the dataset tagged with the version.

## The datasets

| Families | Module | Scenarios |
| --- | --- | --- |
| Turbine fault diagnosis, gearbox diagnostics, vibration analysis | M12.3 | Ten each, in every split, at least four held out with two kinds of fault among them |

The diagnosis scenarios ask what is wrong from what a person would write, never the answer's own words. They include
healthy turbines a careless reading calls faulty (strong wind, a poor data link), sensors that drift alone, and
questions about how far vibration data can be trusted. Two expectations help judge a model fairly:

- **Alternatives:** a diagnosis term may give them, `imbalance|unbalance`, so a right answer in other words passes.
- **`max_confidence`:** where the evidence is limited, the most confidence the result may claim. The **uncertainty**
  evaluator judges it on Claude (EVL-007).

```bash
uv run pytest tests/test_evaluation.py   # every scenario valid, the splits apart, the lock, and each evaluator
make scenarios                           # check, register with the Evaluation Service, sync to LangSmith
make scenarios-lock                      # after giving the dataset a new version
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
