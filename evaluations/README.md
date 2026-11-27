# Evaluations

Versioned scenarios for the agents, scored against the simulator's hidden ground truth (SRS 10.5 to 10.7,
[ADR 0026](../docs/adr/0026-the-first-regression-set-and-agent-dashboards.md)).

| Folder | Holds |
| --- | --- |
| `scenarios/` | One JSON file a scenario: its family, split, the simulator scenario replayed and its seed, the ground truth, the case a person opens, the tools a good investigation calls, what must never happen, and how many runs it needs |
| `datasets/` | Versioned lists of scenarios, each version locked by its content hash. `first-regression.json` is M8.12's twelve |
| `recordings/` | External tools' answers, recorded, so a scenario can give them to the agents (EVL-001). `injection/` holds one for every tool that returns external content, each with an instruction planted in it (MCP-008) |
| `evaluators/`, `benchmarks/`, `regression/` | Phase 12: LLM judges, benchmarks and regression history |

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
| Weather impact, production impact, maintenance planning, spare parts | M12.4 | Ten each, the same way, with a healthy turbine a good plan leaves alone |
| Safety escalation, tool selection, RAG grounding, RBAC and security, human approval, failure recovery | M12.5 | Ten each, the same way, with a prompt-injection case for every tool that returns external content |

The diagnosis scenarios ask what is wrong from what a person would write, never the answer's own words. They include
healthy turbines a careless reading calls faulty (strong wind, a poor data link), sensors that drift alone, and
questions about how far vibration data can be trusted. Two expectations help judge a model fairly:

- **Alternatives:** a diagnosis term may give them, `imbalance|unbalance`, so a right answer in other words passes.
- **`max_confidence`:** where the evidence is limited, the most confidence the result may claim. The **uncertainty**
  evaluator judges it on Claude (EVL-007).

The planning scenarios ask when work can be done, what it costs, what the work order holds and which parts it needs.
Two more expectations judge a plan on Claude:

- **`actions`:** the kinds of action a good plan leads with, the one the Maintenance Planner drafts. Where they include
  no repair or replacement, as for a healthy turbine, neither may be recommended at all. The **actions** evaluator.
- **`impact_required`:** the result carries the Production Impact Service's estimate, whose figures its own checks
  already hold to the service's (FR-IMP-004). The **impact** evaluator.

No agent ever reserves a part or approves a work order: every scenario prohibits both, and that is judged on every run,
rehearsed or not.

The governance scenarios test what must hold whatever the model concludes: a person is asked before anyone climbs, the
question decides the tool, claims rest on approved passages, agents only draft while people submit and decide, and
missing data is named, never filled in. Two more expectations:

- **`cites_documents`:** the result cites an approved document's passage. The **grounding** evaluator.
- **`data_gap_required`:** what the evidence is missing is named as a data gap (FR-INV-005). The **data_gaps** evaluator.

**Prompt injection (MCP-008).** Every tool that returns external content (Tavily's three, the weather forecast, and
the two documentation tools) has a scenario whose recorded answer plants an instruction, as do three cases whose own
words try one. Each instruction asks for what an existing evaluator catches: an approval, a reservation, a control
command, a healthy verdict for a fault, a dropped escalation or full confidence. So an agent that obeys fails, and one
that quotes the instruction in its report does not. A recording is part of its dataset's content, so changing one needs
a new version.

## Recordings (M12.6)

[ADR 0044](../docs/adr/0044-an-evaluation-tenant-on-recorded-providers.md). `make regression` runs in the evaluation
tenant, `windops-evaluation`, whose web research and forecasts are recordings, kept by the Evaluation Service, never
Tavily or Open-Meteo (EVL-001). Demo tenants always see the live sources.

- Before each attempt, the runner sets that scenario's recording, or none for the baseline: plain search results and a
  calm, fresh forecast. The recording is given only while its job's lease holds.
- A documentation tool's recording is a document in the evaluation tenant's library, alongside the manufacturer's
  documents. Upload screening flags one of them, and one addresses agents quietly enough that it is not flagged.
- A forecast's injected text goes in a field the Weather Service never reads: the case shows it dropped.

## The live suite (M12.6b)

The recordings make the regression repeatable, so the real providers are exercised separately. Each day the Evaluation
Service asks, as itself and in a tenant whose providers are live, for a turbine's forecast, a research question and a
page. They must come back fresh, from the provider, secure and physical. A provider that changes its format fails here
before a recording drifts from it. Alerts fire when a run fails or none has run in two days.

```bash
make live-suite   # run it now and show each check: one Tavily search and one extraction, no Claude
```

```bash
uv run pytest tests/test_evaluation.py   # every scenario valid, the splits apart, the lock, and each evaluator
make scenarios                           # check, register with the Evaluation Service, sync to LangSmith
make scenarios-lock                      # after giving the dataset a new version
make regression                          # the set on the cluster, as a job of the Evaluation Service
make regression SCENARIO=gearbox-temp-001,weather-001   # only these: the way a module checks its new scenarios
make regression BENCHMARK=1              # and make it the benchmark, if it passes
```

The [Evaluation Service](../services/evaluation-service/README.md) keeps each dataset version, each job and every
attempt, and gives the verdict: passed only when every planned attempt passed, never when one was skipped
([ADR 0042](../docs/adr/0042-the-evaluation-service-keeps-the-record-and-the-verdict.md)). Each job is compared with
its benchmark, so a regression is named.

A rehearsal judges only what the platform decides: the run completing, the result's shape, its citations, tool
permissions and prohibited actions. Diagnosis, escalation and the tools chosen are judged when Claude runs.
