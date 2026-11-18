# ADR 0018: The investigation graph, checkpointed under the run's lease

**Status:** Accepted, 2026-11-19, for M8.4.

## Context

SRS 5.4 sets the investigation workflow's steps. What M8.4 must deliver:
- **FR-INV-007:** pause, resume, cancellation, bounded retries and failure recovery.
- **AGT-003:** a LangGraph interrupt is never the system of record for a human decision.
- **AGT-004 and AGT-005:** a run pins its versions, a resume checks them, and an incompatible resume stops safely.

The phase plan asks for a test that kills a worker mid-run and proves the run resumes from its last checkpoint.

[ADR 0016](0016-agent-runs-as-leased-jobs-in-postgresql.md) made runs leased jobs, and
[ADR 0017](0017-the-claude-policy-as-versioned-configuration.md) put every call to Claude under a versioned policy.
This ADR adds the graph.

Steps 4 to 12 need the agents, their MCP tools and structured results, which arrive in M8.6 to M8.8 and Phase 9.
The person responsible chose, on 2026-11-19, to build the engine now and wire real cases into it once those steps
have content. No real case gets a placeholder report.

## Decision

**The workflow is one LangGraph graph of 13 nodes,** one for each of SRS 5.4's steps 1 to 13:
`receive`, `validate`, `snapshot`, `plan`, `specialists`, `documents`, `research`, `impact`, `aggregate`,
`validate_output`, `policy_checks`, `recommend`, `conclude`.
- **What runs now:** steps 1, 2, 3 and 13 do what the runtime can do today.
- **Placeholders:** steps 4 to 12 each record a data gap naming the module that gives them content. A run's result
  says plainly what was not done.
- **For checks:** step 4 can ask the Supervisor for a one-line plan, a real call to Claude.

**Checkpoints live in the runtime's own PostgreSQL,** through our own LangGraph checkpointer, `RunCheckpointer`. The
stock PostgreSQL saver was not used:
- **Fencing:** every checkpoint write takes the run's row lock and names the claim's lease token, in the same
  transaction. A worker whose run another replica has taken can't write a checkpoint. Its write raises, and it
  stops. The stock saver knows nothing of leases.
- **Tenant isolation:** checkpoints and their pending writes are tenant-owned tables with forced row-level
  security, like every other table here. The stock saver keeps its own schema.
- **No new driver:** it uses the service's existing SQLAlchemy engine, with its TLS and login, and not a second
  database driver.
- **Never changed:** checkpoint rows never change, which a trigger enforces. A task's writes are kept once, and a
  replayed task writing the same thing again is skipped.

**Durability is `sync`:**
- **Each node:** a checkpoint is written after every node, before the next node starts. A worker that stops,
  however it stops, loses at most the node it was in.
- **Resuming:** the next claim reads the newest checkpoint, and LangGraph skips every node it has passed. The
  Supervisor's plan, made before a kill, is read from the checkpoint and never asked of Claude again.

**Steps are recorded twice, for two readers:**
- **For LangGraph:** the checkpoint, to resume from.
- **For people:** the run's steps, for status and live progress (M8.11).

A node that ran again, because its worker stopped between finishing it and checkpointing it, records its step only
once.

**No human decision waits inside the graph (AGT-003).** There is no `interrupt()` and no interrupt point, and a
test checks the compiled graph for both. The graph ends at step 13. The decision arrives later as a durable event,
and starts a run of its own (step 14, Phase 10).

**Pausing (FR-INV-007):**
- **A queued run** pauses at once.
- **A running run** stops at its worker's next heartbeat, cancelling the node it was in. It keeps its newest
  checkpoint and hands its lease back.
- **Attempts:** pausing is not an attempt.
- **Resuming** queues it again, and it carries on from the checkpoint.
- **Stopped workers:** a run asked to pause whose worker stopped is paused on the next claim, not run.
- **Cancelling:** a paused run can be cancelled.
- **Audit:** pausing and resuming are audited.

**Pins (AGT-004, AGT-005):**
- **What is pinned:** a run pins its graph's version and shape (a hash of its nodes, in order), and the prompt,
  tool and skill versions it uses, beside the Claude policy.
- **Not yet:** prompts beyond the plan's, tools and skills join the pins as M8.6 and M8.7 bring them.
- **On resume:** pins that differ from what is deployed end the run as `stale`, for a person to start again from
  the snapshot.

## Consequences

- **Proved on the cluster:** `make workflow-check` runs two replicas.
  - **The kill:** it kills the worker running an investigation with `SIGKILL`, after the Supervisor's plan. The
    run is taken over once the lease runs out and finishes all 13 steps as attempt 2, none done twice. The resumed
    run's first checkpoint follows the killed worker's last, and Claude is asked for the plan only once.
  - **Pause and resume:** a paused run does nothing while paused, and resumed, finishes as attempt 1.
- **Lost work:** work inside a node is lost when its worker stops, and done again. Nodes must be safe to repeat
  until their checkpoint is written. Calls to Claude inside one are paid for twice in that case, and both are
  recorded against the budget.
- **Checkpoints grow:** they're kept for the run's life. Pruning finished runs' checkpoints comes with data
  retention (Phase 14).
- **Not yet wired:** Investigation starts runs, and step 13 sets the case to `pending_review`, in M8.11, once the
  steps have content.
