# ADR 0026: The first regression set, replayed and scored deterministically, and the agent dashboards

**Status:** Accepted, 2026-11-19, for M8.12.

## Context

SRS 10.5 to 10.7 ask for versioned scenarios, synchronized to LangSmith, scored first by deterministic checks
(EVL-003) and gated by SRS 10.7's targets. SRS 10.3 asks for the Agent Operations and LLM Operations dashboards.
Phase 12 brings the full evaluation: the Evaluation Service, 100 to 200 scenarios, LLM judges and CI gates. M8.12 is
the first set.

Two constraints shape it:
- **Spending:** the person responsible deferred the first run on Claude to the Phase 8 exit gate (M8.13), to pay for
  model calls once.
- **Realism:** an investigation reads the platform's own services through MCP. A scenario is only real if its
  telemetry is really in the platform.

## Decision

**Scenarios are JSON files in `evaluations/scenarios/`,** in SRS 10.5's shape, listed by a versioned dataset in
`evaluations/datasets/`.
- **Each names:**
  - its family and split (train, validation or held out, EVL-006);
  - the simulator scenario replayed for it, and that scenario's hidden ground truth (SIM-005);
  - the case a person opens;
  - the tools a good investigation calls, and actions that must never happen;
  - how many runs it needs: five for safety, security, approval and tool scope (EVL-002).
- **Checked on load:** a test loads every file. A scenario is refused if:
  - its seed or ground truth differ from the simulator's;
  - its case title gives the answer away;
  - it expects a tool no agent has;
  - its family needs five runs and it asks for fewer.
- **The first set:** twelve scenarios over six simulator replays, in nine of the thirteen families. The remaining
  four need Phase 9 and 10's services.

**A scenario runs through the real path,** with `make regression`.
- **The replay:** for each simulator scenario, the runner makes a turbine of its own in `north-sea`. It replays the
  scenario's last hour through the gateway's device route, time-shifted to end now, link trouble included. It holds
  the detector off that turbine, so no case opens by itself.
- **The investigation:** the demo operator asks Web BFF to investigate (API-003), and the runner follows the stream to
  its end.
- **What it reads back:** the run's status, result, cost and duration from the Agent Runtime, and every tool call
  from the audit trail by the run's correlation ID. Everything it made is removed at the end.

**Deterministic evaluators score every run (EVL-003).**
- **Always judged, as the platform decides them:**
  - the run completed and the case waits for review;
  - the result has SRS 5.7's shape;
  - every fact and hypothesis cites a reference the result relies on;
  - no tool outside the calling agent's allowlist (SRS 10.7: 0 unauthorized tool executions);
  - no call denied;
  - no prohibited action, on whichever server offers it.
- **Judged only when Claude ran:**
  - the expected tools were called;
  - escalation is what the scenario says;
  - the top hypothesis names the ground truth's terms.

  A rehearsal's scripted model ignores the scenario, so those are not judged, never counted as passed.
- **Not yet:** LLM judges of evidence support and report quality (EVL-004) come with Phase 12.

**How it runs follows Investigation's `WINDOPS_INVESTIGATION_AGENTS`.**
- **Rehearsed,** the default until M8.13: once per scenario, at no cost.
- **On Claude:** each scenario's required number of runs.

**LangSmith keeps the dataset and the scores.**
- **The dataset:** each run synchronizes the scenarios to the `windops-first-regression` dataset, with the ground truth
  only in the reference outputs.
- **The scores:** they are kept as an experiment, with the graph, prompt, tool, skill and policy versions behind them
  (EVL-005).

**Metrics for the dashboards come from where things happen.** Their labels are fixed values, never content.
- **The runtime:**
  - runs queued, by workflow and who asked;
  - runs ended, by workflow and state;
  - each attempt's duration and outcome;
  - each agent's tool calls by tool and outcome, MCP and Sandbox alike;
  - each handoff from the Supervisor.
- **The Claude library:** every call's tokens by kind, cost, latency and stop reason, and, for a call with no
  answer, what failed.

**The dashboards and their alerts:**
- **Agent Operations:** investigation volume, completion and failure rates, tool success, duration, handoffs, retries
  and takeovers.
- **LLM Operations:** tokens, cost, cache hit rate, latency, provider errors, refusals and fallbacks, and retries.
- **Alerts:** an agent's calls to one tool failing for 10 minutes (`WindopsAgentToolFailing`), and Claude costing more
  than $20 in an hour (`WindopsModelCostHigh`).

## Consequences

- **Proved on the cluster:** `make regression` ran all twelve scenarios, rehearsed. Every run passed every judged
  evaluator. The dataset and the experiment were kept in LangSmith, at no cost on Claude.
- **The dashboards:**
  - Every Agent Operations panel's query returned the runs' data.
  - Every LLM Operations panel is valid but empty: rehearsals call no model. It fills when Claude first runs, at M8.13.
  - Both alerts load and evaluate.
- **Retries inside one request** to Claude happen in the Anthropic client and show only as that call's latency. The
  Retries panel counts run attempts tried again.
- **Not yet:**
  - **EVL-001's recorded Tavily and weather answers:** runs use the live services, with Phase 12's recordings to
    replace them.
  - **The other four families** come with their services.
- **Revisit if:** a replay of an hour proves too short for a model to judge a slow fault such as bearing wear.
