# Evaluation strategy

How WindOps AI evaluates its agents, and decides whether they are good enough to release (SRS 10.5 to 10.7). The
targets here are project targets, not industry benchmarks or guarantees of real-world performance.

## What is evaluated

An **investigation**: a case a person opens on a turbine, investigated by the agent team, ending in a validated result
in SRS 5.7's shape. Every scenario replays a simulated turbine whose fault the simulator knows, so each result can be
judged against what really happened.

## The datasets

[evaluations/](../evaluations/README.md) holds the scenarios, each a JSON file. A dataset manifest lists them under a
version.

- **Families (SRS 10.5):** ten scenarios in each of the thirteen families, from turbine fault diagnosis to failure
  recovery, 130 in all at version 1.5. Each family includes a healthy turbine or limited evidence, so a careless answer
  fails.
- **Splits (EVL-006):** train, validation and held out. Each telemetry replay belongs to one split, and no two
  scenarios share a case title, so no prompt is tuned on the final test's data.
- **Locked versions (EVL-005):** a version is locked by its content hash, recordings included. Changing a scenario
  needs a new version. The Evaluation Service keeps every registered version unchanged.
- **Ground truth (SIM-005):** the simulator's record of each replay goes to the evaluation system, never to an agent.

## Where it runs

A regression runs in its own tenant, `windops-evaluation`
([ADR 0044](adr/0044-an-evaluation-tenant-on-recorded-providers.md)).

- **Recorded providers (EVL-001):** that tenant's web research and forecasts come from recordings, so a run is
  repeatable and spends no Tavily credit. A scenario can plant a prompt injection in any tool that returns external
  content (MCP-008). Demo tenants always see the live sources.
- **The live suite (EVL-001):** daily, the Evaluation Service exercises the real Tavily and weather providers through
  the platform, in a live tenant. An alert fires when a run fails, or none has run in two days. A failure here means a
  provider changed or is down, and the recordings may need renewing.

## Two modes

| Mode | Model | Judges | Costs | Use |
| --- | --- | --- | --- | --- |
| **Rehearse** | Scripted: it calls tools in a fixed order and reports a fixed result | What the platform decides | Nothing | Every module, and before any paid run |
| **Claude** | The model policy's models | Everything, LLM judges included | Claude credit | The phase gate, and model or prompt changes |

A rehearsal proves the platform: runs complete, results have the right shape, tools stay in their allowlists, and
nothing prohibited happens. It cannot judge what a model chooses, and says so: those scores are *not judged*, never
passed.

## The evaluators

**Deterministic checks first (EVL-003):** completion, output validity, evidence references, unauthorized and refused
tools, and prohibited actions. On Claude they also check the tools chosen, the diagnosis, escalation, uncertainty, the
actions recommended, the impact estimate, grounding in approved documents, and stated data gaps.

**LLM judges (EVL-004)** run only on a Claude run whose deterministic checks all passed:

- **Evidence support** sees the findings and the evidence they cite. It never sees the ground truth.
- **Report quality** sees the question, what a good investigation concludes, what happened, and the report.

Each judge scores 1 to 5 by a versioned rubric in `evaluations/evaluators/`, and passes a run at 4. A judge that gives
no score fails the run.

## Repeats and verdicts (EVL-002)

The Evaluation Service plans every attempt when a job starts. On Claude, each scenario runs its `runs_required`
times, and at least 5 for safety escalation, RBAC and security, human approval and tool selection. A job passes only
when every planned attempt passed. One failure fails it. A skipped, missing or errored attempt leaves it incomplete,
never passed.

## The dimensions (EVL-007)

Each report gives every dimension: how many attempts it judged, and how many passed.

| Dimension | Measured by |
| --- | --- |
| Task success | Completion, diagnosis, the action recommended, and the report-quality judge |
| Tool correctness | The tools expected, none outside an allowlist, none refused |
| Grounding | Evidence references, approved-document citations, and the evidence-support judge |
| Output validity | SRS 5.7's shape |
| Consistency | Whether a scenario's repeated runs all agree |
| Uncertainty | Confidence within what limited evidence allows |
| Escalation | Escalated exactly when a person must look |
| Policy compliance | No prohibited action, and the Production Impact Service's figures where asked |
| Cost | What the attempts spent, in all and per attempt |
| Resilience | The failure-recovery scenarios, where data is missing or broken |

## The gates (SRS 10.7)

`make eval` runs a split, held out by default, as one job, and then decides these gates from it:

| Gate | Target | From |
| --- | --- | --- |
| Structured-output validity | 100% | `output_valid` on every attempt |
| Unauthorized tool executions | 0 | `unauthorized_tools` on every attempt |
| Approval bypasses | 0 | No attempt approved, submitted or reserved |
| Evidence references | All valid | `evidence_references` on every attempt |
| Critical safety escalation | 100% across all repeats | `escalation` on every safety-escalation attempt |
| Benchmark task success | At least 90% held out | Held-out attempts on Claude that passed every evaluator and judge |

A report passes only when its job passed and no gate failed. On Claude, every gate must also have been judged. The
other SRS 10.7 gates are proven elsewhere: calculations and business rules by their unit and boundary tests,
circuit-breaker fallbacks by Phase 13's fault injection, and security findings at release.

## Results

Each report is written to `evaluations/regression/<dataset>/<version>/<job>.json` and `.md`. It records:

- the dataset, its version and splits, and the mode;
- every version the runs depended on: graph, prompts, tools, skills, the model policy, and the judges' rubrics;
- time, tokens and cost.

`make eval-report JOB=...` rebuilds a finished job's report at no cost. Each job is also compared with its benchmark,
which names what regressed. Scores are kept in LangSmith as an experiment on the dataset's version.

## When to run what

| When | Run | Cost |
| --- | --- | --- |
| Every pull request | `make check`, which runs the evaluators' and gates' unit tests in CI | Nothing |
| A module that adds or changes scenarios | `make regression SCENARIO=a,b`, rehearsed: only those scenarios | Nothing |
| A change to platform code every run uses | A rehearsed sample, or the full set | Nothing |
| The phase gate | The full set rehearsed as the benchmark, then `make eval` on Claude | Claude credit |
| Before any paid run | The free dry runs: a rehearsal, and the judges' `--count` | Nothing |

A paid run is never started without the account holder's approval, an estimate, and a cost cap below the balance.

## Limits

- A rehearsal says nothing about a model's judgement. Only a run on Claude does.
- The judges are a model too. Their rubrics are versioned so a score can be traced, but they are not ground truth.
  The deterministic checks and the simulator's record are.
- Scenarios are synthetic. Real-world performance needs real operations data, which this project does not have.
