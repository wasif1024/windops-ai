# ADR 0015: The agent stack's vendor checks

**Status:** Accepted, 2026-11-18, for M8.1.

## Context

SRS 13.4 asks Phase 8 to confirm, before anything is built on them:
- that Claude's features work through LangChain and Deep Agents;
- how Claude handles data;
- that LangSmith Sandbox is available on the chosen plan (SBX-004).

The model policy is SRS 5.2: Opus 5.5 for the agents and evaluators, and Haiku 5.5 for metadata extraction. LLM-001
to LLM-010 set what each call must do.

`make vendor-check` confirms each item against the real services:
- **Recording:** it records every request to Claude as sent, and every response as it arrives, so what the libraries
  do is seen, not assumed.
- **What it sends:** only synthetic text, with no key printed.
- **What it costs:** a few cents of tokens and a minute of Sandbox compute.

## Decision

**The stack, pinned:** releases at least two weeks old when chosen. The Agent Runtime takes the same pins when it first calls Claude (M8.3).

| Package | Version |
| --- | --- |
| `anthropic` | 2.4.0 |
| `langchain` | 1.4.9 |
| `langchain-anthropic` | 1.7.10 |
| `langchain-core` | 1.7.5 |
| `langgraph` | 1.2.19 |
| `deepagents` | 0.7.32 |
| `langsmith` (with `sandbox`) | 0.14.17 |

`mcp` stays at 1.30.0 ([ADR 0014](0014-mcp-tools-behind-per-call-tool-tokens.md)).

**Claude through LangChain and Deep Agents: confirmed.** The fallback in SRS 13.4, a thin Claude adapter below the
harness, isn't needed.

| Requirement | What the check saw | How agents use it |
| --- | --- | --- |
| Effort set explicitly (LLM-001, LLM-003) | `effort` reaches `output_config.effort` on Opus 5.5 and Haiku 5.5. LangChain also sends adaptive thinking, the only mode Opus 5.5 accepts | Each agent's model and effort come from versioned configuration (M8.3) |
| Refusal fallback (LLM-006) | Opus 5.5 accepts `fallbacks: "default"` with the `server-side-fallback-2026-07-01` beta, sent through LangChain's `model_kwargs`. A fallback block in history is sent back unchanged | Every Opus 5.5 call carries it, and a fallback is recorded in the trace (M8.9) |
| Structured results (LLM-004) | Opus 5.5 refuses forced `tool_choice`. `with_structured_output(..., method="json_schema")` returns a validated model through `output_config.format`. Strict tools are called with valid arguments | Agents always pass `method="json_schema"`. LangChain's default method would only warn, not force |
| Stop reasons (LLM-005) | `stop_reason` reaches `response_metadata`, as does `max_tokens`. LangChain's source passes `stop_details` too. `pause_turn` comes only from server tools, which agents don't use | Every handler checks `stop_reason` first (M8.3) |
| Prompt caching (LLM-007) | A stable system prompt was read back from the cache, 21,582 tokens. Deep Agents marks every request for caching | Stable prompts and tools come first, and case data after the breakpoint |
| Append-only history (LLM-008) | Each request replays the previous response's blocks unchanged, signed thinking blocks included. This held through LangChain and through a Deep Agents run resumed from its checkpoint | The rule is tested again on every checkpoint and compaction path (M8.4) |
| Private reasoning (LLM-009) | LangChain asks for thinking `display: "summarized"` | Thinking summaries are never stored as audit records, and never shown as an explanation |

**Deep Agents needs four restrictions (M8.4, M8.7):**
- **A default model:** without a `model`, it uses `claude-sonnet-4-6`, against SRS 5.2. Every agent and subagent
  names its model.
- **Default tools:** `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep` and `task`, plus
  `execute` with a sandbox backend. SRS 9.5 forbids agents unrestricted filesystem or shell tools. They are removed,
  or confined to the agent's own scratch state, with no host filesystem. The Sandbox is reached only through the
  Agent Sandbox Adapter's registered tasks.
- **Summarization middleware:** by default it rewrites earlier history when context grows, which breaks LLM-008 and
  invalidates thinking blocks. The Agent Runtime excludes it. If context must shrink, it uses Claude's server-side
  compaction, which is append-only.
- **The general-purpose subagent:** it is added automatically, with the main agent's tools. It is replaced by the
  six named agents (SRS 5.3), each with its own allowlist (M8.6).

**Claude's data handling suits the synthetic data used until Phase 15:**
- **Retention:** inputs and outputs are deleted within 30 days by default (Anthropic's retention terms, updated
  2026-07-01). Longer retention applies only to flagged content, feedback, legal holds and the Files API, which
  agents don't use.
- **Training:** the Commercial Terms bar training on customer content.
- **Region:** the check's calls ran with `inference_geo: global`. The API offers `global` or `us`, with no Saudi
  region.

Data residency for a Saudi deployment is decided with the embedding provider's, in M15.10
([ADR 0011](0011-openai-embeddings-through-the-adapter.md)). Until then only synthetic data reaches Claude, after the
classification checks in SRS 9.3.

**LangSmith tracing works.** A synthetic trace reached the project and was read back. Traces carry real case data
only after the redaction library and its leakage tests (M8.9, EGR-003).

**LangSmith Sandbox is available on this plan (SBX-004).** It runs the first approved task, a vibration spectrum,
and holds no credentials: none of its variables is named like one, and no key of this machine's is in it.

**SBX-002's "no default network" is met only in part. The person responsible accepted this on 2026-11-18, with
compensating controls.**
- **What is closed:** with the sandbox's proxy set to deny every destination, nothing outside LangSmith's managed
  allowlist is reached. An ordinary website and a file-sharing site are refused.
- **What stays open:** the managed allowlist itself, package registries and model APIs. Neither the sandbox's
  allow-list nor its deny-list closes it on this plan. Closing it needs an Enterprise egress exemption or a
  self-hosted Sandbox.

Accepted, as these controls stand in for it:
- **Nothing to authenticate with:** the sandbox holds no credentials, so a model API answers only 401, and nothing
  can be published to a registry.
- **Minimum data:** it gets only the minimum sanitized data an approved task needs (SRS 9.3).
- **Registered tasks only:** each has input, time, memory and output limits (SBX-001).
- **Output checked:** the adapter validates output against the task's schema, and it is labelled supporting
  analysis, never a deterministic rule's input (SRS 5.9).
- **Recorded:** code, input hash, output, runtime and task version go into the trace (SBX-003).
- **Every create denies everything it can:** the most each sandbox can be restricted, so any later plan change takes
  effect at once.

## Consequences

- **The gap stays visible.** `make vendor-check` reports it on every run, as a gap rather than a failure.
- **Revisit if:**
  - the plan changes;
  - LangSmith lets a workspace close the managed allowlist;
  - a task would need data beyond synthetic or sanitized snapshots.
- **New releases:** M8.2 to M8.13 build on the pinned versions. A newer release is taken only after `make
  vendor-check` passes on it.
- **Settings:** `.env` names `LANGSMITH_PROJECT`. A single `ANTHROPIC_MODEL` setting is not how models are chosen:
  each agent's model and effort is versioned configuration (LLM-001, M8.3).
