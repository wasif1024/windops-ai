# ADR 0016: Agent runs as leased jobs in PostgreSQL

**Status:** Accepted, 2026-11-19, for M8.2.

## Context

ARC-006 and ARC-007 ask for:
- the agents to stay inside one Agent Runtime, which scales by adding worker replicas;
- long investigations to run as queued jobs, so that a replica can fail without losing the run.

SRS 5.5 makes the Agent Runtime the owner of run progress and checkpoints, kept in PostgreSQL. Others may only read
status. SRS 5.6 sets the terminal states, and FR-INV-007 asks for pause, resume, cancellation, bounded retries and
recovery. AGT-004 pins each run's versions.

Three places could hold the queue:
- **JetStream**, with a work-queue stream: it already carries events. But a run lasts minutes. JetStream would need
  acknowledgement deadlines of that length, or a worker that keeps extending them. Its progress would also live in a
  second store, beside the checkpoints. And SRS 7.6 fixes the subjects, with none for runs.
- **Redis**: it holds caches and limits that may be lost, never state that must survive.
- **PostgreSQL**: it already holds the run's progress and, from M8.4, LangGraph's checkpoints.

## Decision

**The queue is the `runs` table in the runtime's own database.** It is tenant-owned, with row-level security. A run
is a row, written before `StartRun` answers. So the run waits through any outage of a worker or of the whole runtime,
and queuing a run and recording its progress are the same store.

**A worker claims a run under a lease:**
- **Claiming:** each replica runs a few slots, two by default. A slot claims the oldest due run with
  `FOR UPDATE SKIP LOCKED`, so two workers never take the same one. Claiming is the one place the runtime works
  across tenants. Everything else names the tenant.
- **Renewing:** the claim sets a lease of 30 seconds, renewed every 10 while the run works. The renewal also reads
  whether the run was cancelled.
- **Fencing:** each claim gets a random token, and every write the worker makes names it: a step, a renewal, the end.
  A worker that was frozen, cut off or slow, and whose run another replica has taken, can change nothing. Its next
  renewal finds the lease gone, and it stops.

**How a run carries on:**
- **A crash:** a worker that crashes, freezes or loses the database stops renewing. Once its lease runs out, any
  replica claims the run, as its next attempt.
- **A shutdown:** a worker being shut down hands its run back at once. That is not counted as an attempt, so a
  rolling update costs nothing.
- **Steps:** each completed step is kept, and never changed (a trigger refuses it). A run claimed again skips the
  steps already done. From M8.4, LangGraph's checkpoints resume inside a step; a step is the unit that is never done
  twice.

**Retries are bounded (FR-INV-007):**
- **Attempts:** each claim is an attempt, up to three by default.
- **Failure:** a failed attempt is queued again after a pause that grows with each attempt. A run with none left
  fails, its reason kept. So does a run whose last attempt's worker stopped: it is never claimed a fourth time.

**The terminal states are SRS 5.6's.** A run in one never changes again.

| State | When |
| --- | --- |
| `completed` | Every step is done |
| `failed` | No attempts are left |
| `cancelled` | A queued run at once; a running one at its worker's next renewal |
| `stale` | Not started within 24 hours, or its workflow version is no longer available (AGT-005) |
| `budget_exhausted` | Set by the Claude policy, from M8.3 (LLM-010) |

**Each run pins its workflow and version when queued (AGT-004).** A worker never continues a run on another version.
It ends the run as `stale`, for a person to start again from the snapshot. Until the investigation workflow arrives
in M8.4, the only workflow is `rehearsal`: timed steps, with no model and no tools.

**Only the Investigation Service may call the runtime.** It owns cases, and decides for each caller whether they may
start, read or cancel a case's runs (SRS 5.5).
- **Callers:** the runtime checks the caller's certificate, not a permission, and lets in only
  `windops-investigation`. Web BFF's live progress (M8.11) will be added to that list.
- **Tenant:** it always comes from the verified identity.
- **Retries:** `StartRun` takes an idempotency key.
- **Audit:** queuing, cancelling and each run's end are audited through the outbox.

**Replicas:**
- **Base and production:** the base runs two replicas, with a disruption budget of one.
- **Local:** the local overlay runs one, as K8S-007 asks. `make runtime-check` scales it to two, with a 10-second
  lease, and restores both afterwards.
- **Autoscaling:** not added yet. K8S-003 asks for autoscaling of Agent Runtime workers. It is added once the
  investigation workflow gives a load to measure (M8.12). Until then, replicas are set by hand.

## Consequences

- **A takeover takes up to one lease,** 30 seconds by default. A shorter lease notices sooner, but renews more
  often. It is a setting, `WINDOPS_RUNTIME_LEASE_SECONDS`.
- **Claiming polls:** each slot asks every second, with jitter. At this scale that is a few small queries a second.
  If it grows, PostgreSQL's `LISTEN`/`NOTIFY` can wake the slots instead.
- **Work inside a step is lost when a worker stops,** and done again by the next attempt. So steps must be safe to
  repeat until they complete. M8.4's checkpoints make most of that work resumable, and tool calls that change
  anything carry idempotency keys (GRP-007).
- **Investigation starts runs in M8.4,** when there is a workflow worth running. Until then only `runtime-check`
  queues runs.
