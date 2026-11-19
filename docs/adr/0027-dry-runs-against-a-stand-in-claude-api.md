# ADR 0027: Dry runs against a stand-in Claude API, before any is paid for

**Status:** Accepted, 2026-11-19, for M8.13.

## Context

The Phase 8 exit gate is the agents' first run on Claude itself. The person responsible asked that every other path be
proven first, at no cost, so that money is spent only on what a model decides.

A rehearsal (ADR 0021) replaces each agent's model with a script, inside the process. So it never exercises the path
to Claude:
- the Claude client, its HTTP requests and the tools bound to them;
- adaptive thinking replayed through the history guard;
- step 10's structured outputs;
- the budget and the ledger;
- the LLM metrics;
- the trace's model runs.

Earlier vendor checks (ADR 0015, ADR 0017) proved each feature against the real API on its own, never the whole team
together.

## Decision

**A dry run takes the whole team through the real Claude client, against a stand-in API.**
- **Where it runs:** the stand-in answers Anthropic's Messages API, inside the Agent Runtime's own pod, on 127.0.0.1
  only. It runs only when the runtime is started with `WINDOPS_AGENT_STAND_IN=on`, which `.env` sets locally and
  nothing else does.
- **How a run asks for it:** with `stand_in=true`, which Investigation sends when `WINDOPS_INVESTIGATION_AGENTS` is
  `stand_in`. That run's Claude client gets the stand-in's address and a placeholder key, never the real one.
- **What it plays:** the rehearsal's moves, as real API answers with signed thinking blocks.
  - The Supervisor reads the evidence and hands work to every specialist at once.
  - Each specialist makes its one free tool call and answers.
  - Step 10 gets a result that cites only what the brief listed.

  It decides nothing a model would.
- **Its metrics:** its calls are labelled `provider="stand-in"`, and the LLM Operations dashboard counts only
  `provider="anthropic"`.
- **How it is judged:** a dry run is scripted, so like a rehearsal it is judged only on what the platform decides.

**The exit gate runs in either mode:** `make phase-8-gate MODE=stand_in` is free. `MODE=claude` also needs
`SPEND=yes`, and the person responsible's approval first.
- **Investigation's mode:** the gate sets it for the run, and always puts it back to `rehearse`.
- **Every attempt's trace:** it reads them all, as a retried run resumes from its checkpoint and its later traces hold
  only the steps they ran.

**gRPC connections now find a pod that has gone.**
- **What the dry run found:** after the gate restarted Investigation to switch its mode, the Policy Service could not
  reach it for minutes. Step 11 failed three times, and the run failed. A rehearsal reproduced it, so it was never
  about Claude. On a paid run it would have thrown away everything steps 1 to 10 cost.
- **The cause:** Policy's connection still pointed at the old pod, and with no keepalive nothing noticed. Investigation
  never saw its calls.
- **The fix:** every channel now pings every 20 seconds and drops a connection that doesn't answer within 10. Every
  server accepts those pings.
- **Proved:** after the same restart, the next run passed step 11 at its first attempt.

## Consequences

- **Proved at no cost:**
  - A local test takes the team through the real client over HTTP to the stand-in: every specialist delegated to,
    each tool call with its own agent token, step 10's structured result passing its checks with no repair, and every
    call recorded with its role's model and effort.
  - On the cluster, `make phase-8-gate MODE=stand_in` proves the whole gate.
- **Rollout:** every image was rebuilt for the keepalive, as old servers would refuse pings this frequent.
- **Correction (M9.0):** the server option here was misspelt, and gRPC ignored it. [ADR 0028](0028-agents-read-the-frozen-telemetry-summarized.md) uses gRPC's own name, and a test checks every option name against the library.
- **What a dry run cannot show:** whether Claude follows its prompts, cites well, needs repairs, or fits its budget.
- **The paid run:** before it, Anthropic's free token count accepted every real request the client builds. An
  environment may cap each run below the policy's budget with `WINDOPS_INVESTIGATION_COST_BUDGET_USD`, as the account's
  credit was below the policy's $10. The one investigation on Claude:
  - cost $4.09, under a $6.00 cap;
  - gave a validated, cited result and the right diagnosis.

  It is recorded in the [Phase 8 exit gate](../phases/phase-08-exit-gate.md).
