# ADR 0024: Registered analysis tasks in LangSmith Sandbox

**Status:** Accepted, 2026-11-19, for M8.10.

## Context

Some evidence is better analysed by computation than by a model:
- a vibration signal's spectrum, which RMS cannot give;
- a temperature trend, and when it would reach an alarm level.

SRS 5.9 allows that computation in LangSmith Sandbox, through an Agent Sandbox Adapter, as supporting analysis that
never replaces a deterministic rule. It requires:
- **SBX-001:** only registered task types, each with input, time, memory and output limits;
- **SBX-002:** no credentials or default network access in the Sandbox;
- **SBX-003:** the code, input hash, output, runtime and task version recorded in the trace.

SRS 9.5 forbids agents unrestricted shell or code execution, and Deep Agents' own `execute` tool is excluded
(ADR 0021). [ADR 0015](0015-agent-stack-vendor-checks.md) found the Sandbox available on this plan (SBX-004). It also
found that its managed allowlist (package registries and model APIs) cannot be closed, and that was accepted with
compensating controls.

## Decision

**An agent names a task; it never sends code.**
- **Where tasks live:** each is a folder in `services/agent-sandbox-adapter/tasks/`. `task.yaml` holds its version,
  description, limits, and the JSON Schemas of its input and output; `main.py` holds its code, standard library only.
- **Startup:** the adapter loads them and refuses any task that:
  - has an open schema;
  - has a limit above a fixed ceiling;
  - is named other than its folder.
- **Changing a task:** changing its code or schema needs a new version. Its code hash goes with every result.
- **The two tasks:** `vibration_spectrum` (strongest frequencies, by FFT) and `trend_fit` (least-squares trend, and
  hours to a threshold).

**Limits, before and during a run (SBX-001):**
- **Before a sandbox is made:** input over its byte limit, input that isn't JSON, and input that breaks the task's
  schema are refused. Its schema is closed, so a `code` field is refused like any other unknown field.
- **Each run's sandbox:** one vCPU and 2 GiB, the least LangSmith allows. It requires a sandbox's memory within 50% of
  4 GiB per vCPU, so a task's own memory limit can't be the sandbox's.
- **The task's own limits:**
  - **memory:** `ulimit -v` on its process;
  - **time:** coreutils `timeout`, with the client's command timeout as a backstop;
  - **output:** `head -c`, one byte past the task's limit.
- **What comes back:** output must pass the task's schema, or the run is `failed`. A run past a limit is
  `limit_exceeded`, with no output.

**No credentials or network it can name (SBX-002):**
- **A fresh sandbox each run:** created for that run and deleted after it, whatever happened. Its idle limit deletes it
  if the delete fails.
- **Nothing given to it:** no secret, mount, variable or access delegation. The task runs under `env -i`, with only a
  PATH and HOME.
- **Network:** its proxy allows one destination that cannot resolve, so it denies every destination it can name.
- **The LangSmith key:** only the adapter's pod mounts it. No sandbox ever sees it.
- **The managed allowlist** stays open, as ADR 0015 accepted.

**Who may run what:**
- **Callers:** only the Agent Runtime's certificate may call the adapter. The adapter keeps no state and writes no
  audit records.
- **Which agent:** the runtime gives a task only to an agent whose allowlist names `sandbox.<task>`. That is only the
  Reliability Engineer, as SRS 5.3 lists sandbox analysis among its tools.
- **One list:** the allowlist is the same `AGENT_TOOLS` the Identity Service enforces. A test keeps it matched to the
  adapter's registered tasks, and to each agent's definition.
- **No agent tokens:** none is minted for a Sandbox task, as the adapter is not an MCP server. The Identity Service
  already refuses any audience that isn't one.

**What ran is recorded (SBX-003):**
- **In the trace:** the adapter answers with the task's version, its code and code hash, the input and output hashes,
  its limits, and the runtime. The runtime's tool returns them as the tool message's artifact, which the run's
  LangSmith trace keeps, redacted like everything else ([ADR 0023](0023-agent-traces-redacted-before-export.md)). The
  model never sees the artifact, so it costs no tokens.
- **What the agent sees:** the output, labelled supporting analysis that is never a rule's input.
- **In the audit trail:** each use is audited by the runtime, under the run's lease, as the agent on the user's
  behalf, with the run's ID as its correlation ID (OBS-008).

**An unavailable Sandbox doesn't stop the run.**
- **Resilience:** the adapter calls the Sandbox through a circuit breaker, and runs at most two sandboxes per replica
  at once.
- **When it is unavailable:** the agent is told the analysis couldn't be run, and carries on without it (SRS 7.7).

## Consequences

- **Proved by `make sandbox-check`:**
  - only the runtime's certificate is let in;
  - the deployed tasks are the repository's, by code hash;
  - bad input never reaches a sandbox;
  - both tasks answer correctly;
  - runaway tasks are stopped by their time, memory and output limits in the real Sandbox;
  - a task's environment holds no credentials and reaches no ordinary website;
  - a traced rehearsal's LangSmith trace holds what ran, and its audit record names the agent, the user and the run.

  It costs a few sandboxes' seconds of LangSmith compute, and nothing on Claude.
- **Rehearsals stay free:** a run asks for a real task with `sandbox=true`, which only a rehearsal accepts.
- **A new task:** a folder, a reviewed version, an entry in `SANDBOX_TASKS`, and the agent definitions that need it.
- **Revisit if:**
  - a task needs data beyond sanitized snapshot values;
  - the plan lets a workspace close the managed allowlist;
  - LangSmith allows a smaller sandbox.
