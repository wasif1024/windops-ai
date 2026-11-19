# ADR 0023: Agent traces in LangSmith, redacted before export

**Status:** Accepted, 2026-11-19, for M8.9.

## Context

What agent traces must capture:
- **OBS-005:** the investigation, tenant, user and correlation IDs, agent runs, handoffs and workflow transitions.
- **OBS-006:** provider, model, effort, prompt version, tool calls with sanitized arguments, outcomes, and
  references.
- **OBS-007:** latency, token usage, cache use, estimated cost, retries, refusals and policy denials.
- **OBS-008:** LangSmith and the Audit Service share correlation IDs, and the Audit Service stays the authoritative
  record.

What must never leave the cluster:
- **EGR-003:** a redaction library runs before LangSmith export, covered by leakage tests.
- **OBS-003:** traces never include secrets, tokens or unnecessary personal data.
- **LLM-009:** private reasoning is never stored.

Until now `LANGSMITH_TRACING` was off in the Agent Runtime, since M8.3, because none of this existed. The person
responsible asked for full graph tracing, and for LangGraph Studio.

## Decision

**Nothing traces by itself.** `LANGSMITH_TRACING` stays `false`, so no library sends anything of its own.
- **When the runtime traces:** each run explicitly, when `WINDOPS_AGENT_TRACING` is on and its LangSmith key is
  mounted.
- **Where it goes:** to the project in `WINDOPS_LANGSMITH_PROJECT`.
- **The key:** the Secret `windops-langsmith-api-key`, made from `LANGSMITH_API_KEY` by `make secrets`, which only the
  runtime's pod mounts.
- **Without the key:** nothing is traced.

**One client, with redaction in its hooks.** The runtime's LangSmith client runs `redact_trace`, from
`windops-observability`, in its `hide_inputs`, `hide_outputs` and `hide_metadata` hooks, so no value can be exported
unredacted. It removes:
- **Secrets:** passwords, keys, tokens, private keys and credentials, by key and by value.
- **Personal data:** e-mail addresses anywhere, and fields that name people. IDs stay, as OBS-005 needs them.
- **Private reasoning:** thinking blocks, signed or redacted, wherever they appear, including inside LangChain's
  serialized messages. A marker shows one was there.
- **Bulk text:** any string over 4,000 characters is cut, so no whole document, page or evidence item leaves.
- **Live objects:** they are taken apart and redacted field by field, never passed through. The leakage test found
  that the hooks receive live LangChain messages, such as a tool's answer: an earlier draft passed them through, and
  the exporter serialized them, planted token and all.

**What a trace holds:**
- **The root:** one per run, named `investigation`, with the run, case, snapshot, tenant, user and correlation IDs,
  the workflow and the attempt (OBS-005).
- **Beneath it:** every graph step; the Supervisor and each specialist it delegated to, through the `task` tool; and
  every tool call.
- **Each agent's model run:** its agent, model, effort and prompt version (OBS-006). Each call's served model,
  fallback, stop reason, cost, and processed and cached tokens are added to its response metadata for the trace.
  That metadata is never sent back to Claude (OBS-007).
- **Logging:** each export is logged by run and project, never its content (EGR-004).
- **Flushing:** a run's trace is flushed in a thread after it ends, so a slow LangSmith never holds a worker or its
  lease.

**One correlation ID, the run's own (OBS-008):**
- **On the trace:** it is the root's `correlation_id`.
- **On the runtime's records:** every `agent_run.*` audit record carries it.
- **On every tool call:** the runtime passes it when it asks for an agent token, and the Identity Service signs it into
  the token as `cid`. Each MCP server then puts it on that call's audit record.

So any trace joins straight to the authoritative audit trail.

**LangGraph Studio is not set up.**
- **Why:** its local server, `langgraph-api`, requires protobuf below 7. Our gRPC contracts need protobuf 7, and
  Studio's server must import our graph, which imports them.
- **What stands in for it:** LangSmith's trace view shows the same thing, each step and agent as one nested tree.
- **When to try again:** once `langgraph-api` supports protobuf 7.

## Consequences

- **Proved for free on the cluster:** `make trace-check` rehearses a traced run on a case whose title carries a planted
  e-mail and API key, which a real `get_case` call returns to the team. It reads the whole trace back from LangSmith's
  own API, and proves:
  - nothing planted, and no token, arrived;
  - the root names every ID;
  - all 13 steps, every agent's model run with its metadata, and every tool call and handoff are in the trace;
  - the MCP audit records carry the run's correlation ID.
- **Unit tests:** leakage tests plant each kind of secret, personal field and thinking block in LangChain's real
  shapes, and check what the client would send.
- **What redaction can't do:** it can't tell a sensitive sentence from an ordinary one. Case data in traces stays
  synthetic until Phase 15, where data residency is decided with the providers' (M15.10).
- **Regulated tenants:** turning tracing off for a tenant whose data may not leave is a setting there. Today it is one
  setting for the whole runtime.
