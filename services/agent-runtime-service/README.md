# windops-agent-runtime

Agent runs, as queued jobs on worker replicas (ARC-006, ARC-007). A replica can stop at any moment, and its runs carry
on elsewhere. The decisions behind it are in [ADR 0016](../../docs/adr/0016-agent-runs-as-leased-jobs-in-postgresql.md).

There are three workflows:
- **`investigation`:** SRS 5.4's steps 1 to 13 as a LangGraph graph, checkpointed after every node
  ([ADR 0018](../../docs/adr/0018-the-investigation-graph-and-its-checkpoints.md)). Step 5 runs the team: the
  Supervisor and its five specialists on restricted Deep Agents
  ([ADR 0021](../../docs/adr/0021-the-agents-and-skills-on-deep-agents.md)), defined in the repository's `agents/`
  and `skills/`. Step 10 turns the Supervisor's report into SRS 5.7's structured result and checks every citation
  against what the tools returned, with at most two repairs; step 11 checks each recommended action through Policy;
  step 12 keeps the result on the run
  ([ADR 0022](../../docs/adr/0022-the-structured-result-checked-against-what-the-team-saw.md)). A run can rehearse the
  team on a scripted model, at no cost. Investigation starts these runs for its cases, follows them, and sets the
  case to `pending_review` when one completes
  ([ADR 0025](../../docs/adr/0025-investigations-started-and-followed-by-their-case-owner.md)).
- **`rehearsal`:** timed steps with no model and no tools, so the queue and its recovery can be proved without
  spending anything.
- **`model-check`:** one short call per role, a structured answer and a cached prompt, a few cents of synthetic
  text, so the Claude policy can be proved against Claude itself.

## A run

| State | What it means |
| --- | --- |
| `queued` | Kept in PostgreSQL before `StartRun` answers. It waits through any outage |
| `paused` | Paused at once if queued, or at its worker's next heartbeat, keeping its checkpoint. `ResumeRun` queues it again |
| `running` | A worker holds it, under a lease it renews every third of the lease while it works |
| `completed` | Every step is done |
| `failed` | An attempt failed with none left, or the last attempt's worker stopped. The reason is kept |
| `cancelled` | Cancelled while queued, at once, or while running, at the worker's next heartbeat |
| `stale` | Not started within a day, or its workflow version is gone (AGT-005): start it again |
| `budget_exhausted` | Its token or cost budget ran out (LLM-010, from M8.3) |

The terminal states are SRS 5.6's, and a run in one never changes again.

## Workers

Each replica runs `WINDOPS_RUNTIME_WORKER_SLOTS` slots, two by default. A slot claims the oldest run that is due, with
`FOR UPDATE SKIP LOCKED`, so two workers never take the same run, and runs it to its end.

| If the worker | Then |
| --- | --- |
| Is shut down | It hands its runs back at once, and another replica claims them. That is not an attempt |
| Crashes, or loses the database | Its lease runs out after `WINDOPS_RUNTIME_LEASE_SECONDS`, 30 by default, and another replica claims the run, as its next attempt |
| Comes back after its run was taken | Every write it makes names its lease's token, so it changes nothing. Its heartbeat finds the lease gone, and it stops |

A run taken over carries on after its last completed step: completed steps are kept, never changed, and skipped. A
failed attempt is tried again after a pause, up to `WINDOPS_RUNTIME_MAX_ATTEMPTS`, three by default (FR-INV-007).

## The API

`StartRun`, `GetRun`, `ListRuns`, `CancelRun`, `PauseRun` and `ResumeRun`. The Investigation Service owns cases and decides who may start, read
or cancel their runs (SRS 5.5), so only its certificate is let in. The tenant comes from the verified identity it
sends. A run pins its workflow and version when queued (AGT-004), and `StartRun` may be retried with the same
`idempotency_key`.

Queuing, cancelling and each run's end are audited.

## Claude

Every call goes through the `windops-claude` library and its versioned policy
([ADR 0017](../../docs/adr/0017-the-claude-policy-as-versioned-configuration.md)).
- **Pins:** a run pins the policy's version and hash when queued, and its tenant's token and cost budget. A caller
  may lower the budget, never raise it.
- **Ledger:** each call is a row in `run_calls`, never changed, with its role, the model asked for and the one that
  answered, effort, stop reason, any fallback, tokens and cost. Only the worker holding the run's lease can add one.
- **Budget:** a run that reaches its budget ends as `budget_exhausted`. A refusal, a cut-off answer or an edited
  history ends it as `failed` at once, as another attempt would only pay for the same answer.
- **The key:** `ANTHROPIC_API_KEY` in `.env` becomes the Secret `windops-anthropic-api-key`, which only this pod
  mounts.

## Sandbox tasks

An agent whose allowlist names `sandbox.<task>` gets a tool for that registered task, run in LangSmith Sandbox by the
[Agent Sandbox Adapter](../agent-sandbox-adapter/README.md) ([ADR 0024](../../docs/adr/0024-registered-tasks-in-langsmith-sandbox.md)).
Today that is only the Reliability Engineer. The agent gives input, never code, and sees the output labelled
supporting analysis. What ran goes in the trace as the tool message's artifact, and each use is audited as the
agent, on the user's behalf. A rehearsal runs a real task only when started with `sandbox=true`.

## Dry runs

With `WINDOPS_AGENT_STAND_IN=on`, the runtime serves a stand-in of Anthropic's Messages API on 127.0.0.1, inside its
own pod. A run with `stand_in=true` takes the whole team through the real Claude client against it, with a placeholder
key, at no cost ([ADR 0027](../../docs/adr/0027-dry-runs-against-a-stand-in-claude-api.md)). Its calls are counted as
`provider="stand-in"`, never as Claude's.

## Traces

Each run is traced to LangSmith through one client whose hooks redact every value before export
([ADR 0023](../../docs/adr/0023-agent-traces-redacted-before-export.md)). The root names the run, case, snapshot,
tenant, user and correlation IDs; beneath it are every step, agent, model call and tool call. The correlation ID is
the run's own, shared by its audit records and every MCP tool call's. `LANGSMITH_API_KEY` in `.env` becomes the
Secret `windops-langsmith-api-key`, which only this pod mounts; without it, nothing is traced.

## Checks

```bash
make runtime-check
make workflow-check
make team-check
make trace-check
make sandbox-check
make progress-check
make phase-8-gate MODE=stand_in
make claude-check
```

It scales the runtime to two replicas, kills one mid-run, and proves the other takes the run over from its last
completed step, along with handing back on shutdown, cancelling, the attempt limit, tenant isolation and who may call.
`workflow-check` kills a worker with SIGKILL mid-graph and proves the run resumes from its last checkpoint, and
proves pausing. `claude-check` makes real calls through the runtime, and proves the policy, the costs and a budget ending a run.
