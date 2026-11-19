# ADR 0025: Investigations started and followed by their case's owner, streamed as numbered events

**Status:** Accepted, 2026-11-19, for M8.11.

## Context

SRS API-003: creating an investigation returns a case ID at once, and progress streams over SSE with resumable
event IDs. Until now the pieces stood apart:
- **The runtime's runs** were started only by checks, holding the Investigation Service's certificate
  ([ADR 0016](0016-agent-runs-as-leased-jobs-in-postgresql.md)).
- **Delegations** were started only by checks too ([ADR 0019](0019-agent-identities-delegations-and-agent-tokens.md)).
- **Step 13** recorded a gap ([ADR 0018](0018-the-investigation-graph-and-its-checkpoints.md)) where it should set the
  case to `pending_review`.

The Investigation Service owns cases and their status (SRS 5.5). The Agent Runtime owns runs and their steps.

## Decision

**Investigation starts each case's run, once the case's first evidence snapshot is frozen.** A run needs a frozen
snapshot (EVD-001), and freezing takes seconds to a minute, so a background keeper in Investigation does it, as the
evidence builder freezes snapshots.

| Case | Who asks | Its agents act for |
| --- | --- | --- |
| From detections | Investigation, for each new case | The tenant's `fault-investigator`, delegated by Investigation acting as itself |
| A person's | `CreateCase` with `investigate`, which Web BFF's `POST /api/v1/investigations` sends | That person, delegated while they wait for the case's ID, with their own Keycloak token |

- **The token:** Web BFF passes the person's verified token on. Investigation hands it to the Identity Service and
  drops it: it is never stored, logged or part of the idempotency hash. A retry with a refreshed token is the same
  request.
- **The delegation** lasts four hours, the most allowed. A run with under 30 minutes of it left is not started, and
  the case says why.
- **Starting** is idempotent: the run's key is the case's, so two replicas, or a retry after an outage, start one run.
- **Not yet:** detection cases opened before this change, and a second investigation of the same case, are not
  started. Phase 10's stale evidence brings new versions.

**Investigation follows each run, and sets the case's status.** The keeper reads each running investigation's run
every two seconds.
- **The case's status:**
  - `investigating` when the run starts;
  - `pending_review` when the run completes, step 13's end (SRS 5.4);
  - back to `open`, with the run's status and reason, when it ends any other way.
- **A cancelled case** cancels its run. A case cancelled before its run started never starts it.
- **Why polling, not a call from the runtime:** the case's owner sets the case. The runtime would otherwise need a
  write path into cases. A missed call would also leave a case stuck, where a missed poll is retried. Two seconds is
  well inside a run's minutes.

**Progress is a log of numbered events per case,** in a tenant-owned table a trigger keeps unchanged.
- **The events:** `requested`, `started`, one `step` for each completed step (its position of 13), `run_ended`, and
  `case_status`.
- **Numbering:** each event's sequence counts from 1 in its case. It is written under the case's row lock, keyed by
  what it records, so two replicas following one run add each step once.

**Streaming:** two hops.
- **Investigation's `WatchInvestigation`:** a gRPC server stream from a given sequence. It checks
  `investigation.read` once, as it starts. It polls the log every second and sends a heartbeat after 10 quiet
  seconds. It ends with `finished` once the investigation is over, or without it after five minutes, to be called
  again.
- **Web BFF's `GET /api/v1/investigations/{id}/events`:**
  - **Format:** passes it on as SSE, each event's sequence as its `id` and its kind as its name. A client sends
    `Last-Event-ID` to carry on after it.
  - **Errors:** a case the person may not read is a 404 before any stream starts.
  - **Length:** a stream lasts no longer than the person's token.
  - **Buffering:** `X-Accel-Buffering: no` keeps the gateway's nginx from holding events back. Heartbeats come every
    10 seconds, inside the route's 15-second read timeout.
  - **The browser:** `EventSource` cannot send an `Authorization` header, so the frontend (M11.1) reads the stream
    with `fetch`. No token goes in a URL.

**What a run costs is a setting.** `WINDOPS_INVESTIGATION_AGENTS`:
- `rehearse`: the real team on a scripted model, with no call to Claude;
- `claude`: the team on Claude;
- `off`: no runs.

Every environment says `rehearse` until the Phase 8 exit gate (M8.13) turns Claude on, so a fault on the cluster
spends nothing by accident.

## Consequences

- **Proved by `make progress-check`:** a real login asks Web BFF, through the gateway, to investigate a turbine.
  - **The answer:** the case's ID comes back at once, and a retry with the same key is the same case.
  - **The run:** Investigation starts it under the operator's own delegation, rehearsed.
  - **The stream:** dropped after four events and resumed with `Last-Event-ID`, it delivers every event once, in
    order. That is the request, the run's start, all 13 steps, the run's end and the case's move to
    `pending_review`. Then it says it is finished.
  - **Isolation:** another tenant's operator gets 404.
- **Load:** each open stream polls Investigation's database once a second. Many watchers would want a push, such as
  PostgreSQL `LISTEN`; that is not needed at this scale.
- **Permission is checked when a watch starts.** A revoked grant stops a watcher within five minutes, when the watch
  is called again.
- **Revisit if:** runs become long enough that a two-second poll matters, or a second investigation of a case is
  needed before Phase 10.
