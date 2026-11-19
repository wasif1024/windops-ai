# Phase 8 exit gate

**Result:** passed on 2026-11-20, on the local Minikube cluster, with the agents on Claude Opus 5.5. The one paid
investigation took 6.6 minutes and cost $4.09.

SRS section 12 sets the gate: *a simulated fault produces a validated, cited report with a full trace.* It is also the
agents' first run on Claude itself, deferred from M8.7 so model calls are paid for once (ADR 0021).

```bash
make phase-8-gate MODE=stand_in            # free: the real Claude client against a stand-in API
make phase-8-gate MODE=claude SPEND=yes    # one investigation on Claude, capped by its budget
```

## Proven free first

Nothing was spent on Claude until everything else had passed
([ADR 0027](../adr/0027-dry-runs-against-a-stand-in-claude-api.md)).

| Check | Result |
| --- | --- |
| `make regression`, rehearsed (M8.12) | All 12 scenarios passed every judged evaluator |
| `make phase-8-gate MODE=stand_in` | Passed in one attempt: the whole team through the real Claude client, over HTTP, against a stand-in API in the runtime's pod |
| Anthropic's token count, which is free | Every real request was accepted: system prompts, tools, thinking, and step 10's structured result schema. A scripted investigation's 14 calls hold 44,996 input tokens |

**The dry run found two bugs before any spend:**
- **Stale connections:** after Investigation restarted, the Policy Service kept calling the old pod's dead connection
  for minutes, so step 11 failed and the run failed. Every gRPC channel now keeps alive, and every service was
  rebuilt with it.
- **Logging:** the stand-in's web server replaced the runtime's JSON logging. It no longer does.

**The run's budget:** `WINDOPS_INVESTIGATION_COST_BUDGET_USD` capped it at $6.00, below the account's credit, and the
dry run's budget confirmed the cap before the paid run.

## The paid run

The gearbox oil overheating scenario's last hour was replayed into a turbine of its own. The ground truth is failing
oil cooling. The demo operator asked Web BFF to investigate it.

```text
info - gearbox-temp-001 run 1 of 1: passed in 396s, 49 tool calls, $4.0851
ok - The run completed and the case waits for review (396s)
ok - It ran the team's model path, not a scripted rehearsal
ok - output_valid: passed
ok - evidence_references: 20 references
ok - unauthorized_tools: 49 calls; outside an allowlist: none
ok - refused_tools: denied: none
ok - prohibited_behavior: done: none
info - expected_tools: True missing: none
info - diagnosis: True ground truth gearbox_oil_overheating; top: a cooler or oil-cooling fault (fan or its capacitor, fouled cooler, stuck bypass valve, or low oil) is causing the hot gearbox
info - the Supervisor handed work to ['production-impact', 'reliability-engineer', 'safety-researcher', 'scada-analyst']
ok - Its trace holds all 13 steps, and 26 agent model runs, every agent that worked naming its model, effort and prompt version (OBS-005, OBS-006)
ok - Each of the 28 calls beneath them records its token use (OBS-007)
ok - It holds the tool calls and handoffs: ['documentation_search_documents', 'read_file', 'task', 'tavily_search_web', 'weather_get_forecast', 'windops_check_policy', 'windops_get_case', 'windops_get_evidence', 'windops_get_latest_telemetry', 'windops_get_turbine', 'windops_list_alarms', 'windops_list_cases']
ok - No token reached the trace
ok - Its root names the run as the correlation ID its audit records share (OBS-008)
ok - The LLM metrics hold its calls, as provider anthropic
```

The trace checks were first run with a rule written for scripted runs: every one of the six agents. Claude's
Supervisor chose not to involve the Maintenance Planner, which is a model's decision. The rule became "every agent
that worked", and `--verify-run` checked the same run's trace again, calling nothing.

**What Claude produced**, run `run_gadklf8yd2yqv4vn1yqj`:
- **Findings:**
  - 12 facts, 5 hypotheses, 1 conflict and 8 data gaps, cited to 20 references;
  - one repair at step 10;
  - escalation required, and confidence 0.3.
- **The diagnosis:** its top hypothesis is an oil-cooling fault, the ground truth. It was ranked jointly with
  high-speed bearing wear.
- **The policy check:** the Policy Service escalated two recommended actions, and blocked the inspection that needs a
  climb.
- **The cost:** 28 calls, 42,448 output tokens and $4.09, within the earlier estimate of $0.70 to $5.20.

## What the run showed needs work

These do not fail the gate. They are why a real run was worth paying for.

- **The agents cannot see the evidence's readings:**
  - **What happened:** the frozen telemetry item holds counts and quality labels, not values. Claude said so, rather
    than inventing them, and marked every hypothesis untested.
  - **Next:** a tool that gives agents each metric's statistics over the frozen window.
- **A forecast cannot be cited:** a forecast's ID (`fct_…`) is not among the references a claim may cite, so the
  result names the forecast only as a gap.
