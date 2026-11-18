# ADR 0017: The Claude policy as versioned configuration, metered per run

**Status:** Accepted, 2026-11-19, for M8.3.

## Context

SRS 5.2 sets each agent's model and effort, and LLM-001 to LLM-010 set what each call must do.
[ADR 0015](0015-agent-stack-vendor-checks.md) confirmed how Claude's features work through LangChain. This decision
turns that into code that every call goes through: the Agent Runtime's agents from M8.7, and later Knowledge's
metadata extraction and Evaluation's judges.

## Decision

**The policy is a file:** `shared/claude/src/windops_claude/claude-policy.yaml`, loaded and checked by the
`windops-claude` library. It holds each role's model, effort and output limit, the prices, and each tenant's
budget. No prompt and no code names a model.

**A change to the policy is a new version.**
- **Locking:** `policy.lock.json` records the content hash of each version. A test fails while the file's hash
  differs from its version's locked hash. So a change needs both a new version and `make claude-policy-lock`, and
  shows up in review.
- **Gates:** a model or effort change must also pass the Section 10 gates before release (LLM-002). Those gates
  arrive in Phase 12.
- **Pinning:** a run pins the version and hash when it is queued (AGT-004). The runtime holds only the current
  policy, so a run whose policy has changed ends as `stale`, to be started again (AGT-005).

| Role | Model | Effort | Output limit |
| --- | --- | --- | --- |
| Supervisor, Reliability Engineer, Maintenance Planner, Safety and Procedure Researcher | `claude-opus-5-5` | high | 16,000 |
| SCADA Analyst, Production Impact | `claude-opus-5-5` | medium | 8,000 |
| Metadata extraction | `claude-haiku-5-5` | low | 2,000 |
| Evaluators | `claude-opus-5-5` | high | 8,000 |

**Every call goes through one client, in this order:**
1. **The budget:**
   - **Spent:** a run that has reached its budget makes no call.
   - **Output limit:** it is lowered to the tokens left, so a call can overshoot the budget by at most its input.
2. **The model:**
   - **From the policy:** the role's model and effort, set explicitly (LLM-003). LangChain sends adaptive
     thinking with them.
   - **Fallback:** Opus 5.5 always carries `fallbacks: "default"` (LLM-006). Haiku 5.5 has no fallback and
     refuses the field, so it is sent only to models that take it.
3. **The history guard:** the request must repeat the conversation so far exactly, thinking blocks byte for byte
   (LLM-008). An edit, a dropped message or a new order is refused before anything is sent.
4. **The call:**
   - **Structured results** use structured outputs (`method="json_schema"`) with the raw message kept, never forced
     tool choice (LLM-004).
   - **Recording:** the call is recorded whatever the answer, because every call is paid for.
5. **The checks:**
   - **The budget again.**
   - **The stop reason,** before any content is read (LLM-005). Only `end_turn`, `tool_use` and `stop_sequence`
     pass. `refusal`, `max_tokens`, `pause_turn` and any unknown reason raise.
   - **Fallbacks:** a fallback, seen from its content block or from `usage.iterations`, is recorded with the call.

**Caching (LLM-007):** a stable system prompt is marked for the cache. It refuses anything that changes between
calls: a date, a time, a record's ID or a UUID. That belongs after the breakpoint.

**Private reasoning (LLM-009):** `public_content` drops thinking blocks from anything kept or shown. They stay only
in the conversation, as LLM-008 needs.

**Prices** come from the pricing page, checked on 2026-11-19. They are kept with their source and date.
- **Opus 5.5:** $4 input and $20 output per million tokens. Cache writes are $5 for five minutes and $8 for an
  hour; cache reads are $0.20.
- **Fallback targets:** Opus 5 and Opus 4.8, at $5 and $25, are priced too, since fallbacks are served by them.
- **Haiku 5.5:** $0.10 and $0.50, and five times that for a prompt over 100,000 tokens.
- **US-only inference:** prices are multiplied by 1.1.
- **No price:** a model the table doesn't list is charged the highest listed rates.
- **Rounding:** costs are rounded up to the micro-dollar. A budget is never under-counted.

**Budgets (LLM-010):**
- **Size:** each investigation gets a token budget and a cost budget, by default 2 million tokens and $10.
- **Per tenant:** a tenant may have its own, in the policy. A caller may lower a run's budget, never raise it.
- **What counts:** tokens count what the model processed: input not read from the cache, cache writes and output.
  Cache reads count only in the cost, so a well-cached run isn't penalized for reusing its prompt.
- **Ledger:** every call is a row in the runtime's `run_calls`, never changed. It is kept across attempts, so a
  retried run can't spend its budget twice. A worker that has lost its lease can't record a call.
- **Ending:** a run that reaches either budget ends as `budget_exhausted`.
  - **Audit:** its end is audited.
  - **Alert:** `WindopsAgentBudgetExhausted` alerts a person until the Notification Service takes this over
    (M10.5).

**Failures that won't change on another attempt end the run at once,** as `failed`: a refusal after fallback, a
cut-off or malformed answer, an edited history, or no key. Trying again would only pay for the same answer.

**The key:**
- **Where it comes from:** `ANTHROPIC_API_KEY` in `.env` becomes the Secret `windops-anthropic-api-key`.
- **Who mounts it:** only the runtime's pod. It is read as a file at each call, so a replaced key needs no restart.
- **Without it:** a run that needs Claude fails, saying so.
- **Tracing:** LangSmith tracing stays off in the runtime until traces are redacted (M8.9).

**The library pins the agent stack exactly,** as `make vendor-check` confirmed it (ADR 0015).

## Consequences

- **Proved on the cluster:** `make claude-check` makes real calls, a few cents of synthetic text, through the
  `model-check` workflow. It proves each role's model and effort, a cached read, the costs, a budget ending a run,
  its audit record and its alert.
- **Prices go stale:** they are configuration, kept with their date. A price change is a policy change, with a new
  version.
- **Data residency:** the API default, `global`, is used. Saudi residency is decided in M15.10, and US-only
  inference is priced in already.
- **Repairs:** at most two output repairs per result come in M8.8. Until then a malformed structured answer fails
  the run.
