# windops-agent-runtime

Agent runs, as queued jobs on worker replicas (ARC-006, ARC-007). A replica can stop at any moment, and its runs carry
on elsewhere. The decisions behind it are in [ADR 0016](../../docs/adr/0016-agent-runs-as-leased-jobs-in-postgresql.md).

The investigation workflow itself comes in M8.4. Until then the only workflow is `rehearsal`: timed steps with no
model and no tools, so the queue and its recovery can be proved without spending anything.

## A run

| State | What it means |
| --- | --- |
| `queued` | Kept in PostgreSQL before `StartRun` answers. It waits through any outage |
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

`StartRun`, `GetRun`, `ListRuns` and `CancelRun`. The Investigation Service owns cases and decides who may start, read
or cancel their runs (SRS 5.5), so only its certificate is let in. The tenant comes from the verified identity it
sends. A run pins its workflow and version when queued (AGT-004), and `StartRun` may be retried with the same
`idempotency_key`.

Queuing, cancelling and each run's end are audited.

## Checks

```bash
make runtime-check
```

It scales the runtime to two replicas, kills one mid-run, and proves the other takes the run over from its last
completed step, along with handing back on shutdown, cancelling, the attempt limit, tenant isolation and who may call.
