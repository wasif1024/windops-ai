# Agent Sandbox Adapter

Runs registered analysis tasks in LangSmith Sandbox, for agents
([ADR 0024](../../docs/adr/0024-registered-tasks-in-langsmith-sandbox.md), SRS 5.9).

- **Registered tasks only (SBX-001):** an agent names a task and gives it input; it never sends code. Each task is a
  folder in [`tasks/`](tasks/): `task.yaml` gives its version, limits, and the JSON Schemas of its input and output,
  and `main.py` is its code.
- **Limits:** input size and schema are checked before a sandbox is created. The sandbox gets one vCPU and 2 GiB, the
  least LangSmith allows; the task's own memory limit is set on its process by `ulimit -v`. The command gets the
  task's time, and output past the task's limit is cut and refused. Output must pass the task's schema.
- **No credentials or default network (SBX-002):** each run gets a new sandbox, deleted after it. It is given no
  secret, mount or variable, the task runs under `env -i`, and its proxy denies every destination it can name.
  LangSmith's managed allowlist stays open on this plan, accepted in [ADR 0015](../../docs/adr/0015-agent-stack-vendor-checks.md).
- **What ran (SBX-003):** each answer carries the task's version, its code and code hash, the input and output hashes
  and the runtime, which the Agent Runtime records in the run's trace and audit trail.
- **Callers:** only the Agent Runtime's certificate. The runtime gives a task only to an agent whose allowlist names
  it.
- **Supporting analysis:** a result is never the input to a deterministic rule (SRS 5.9). If the Sandbox is
  unavailable, the agent is told so and carries on without it.

| Task | What it gives |
| --- | --- |
| `vibration_spectrum` | The strongest frequencies in a vibration signal |
| `trend_fit` | A straight-line trend through readings, and when it would reach a threshold |

The LangSmith key is `LANGSMITH_API_KEY` in `.env`, made into the Secret `windops-langsmith-api-key` by `make
secrets`. Only this service and the Agent Runtime mount it; no sandbox does.

```bash
make sandbox-check
```

It costs a few sandboxes' seconds of LangSmith compute, and nothing on Claude.
