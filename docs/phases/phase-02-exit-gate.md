# Phase 2 exit gate

**Result:** passed on 2026-11-05, on the local Minikube cluster.

SRS section 12 sets the gate: *an event survives a pod restart, and a redelivered audit event creates only
one record.* Run it yourself with:

```bash
make cluster
make phase-2-gate
```

## How it was run

`make clean` first removed all WindOps workloads, namespaces and data. The check then ran `make images`,
`secrets`, `infra`, `migrate` and `services`, in the order SRS 11.4 sets. New volumes were created, so no earlier
data could affect the result.

## Evidence

Output from the run:

```text
The documented commands deployed the data stores and the Audit Service
The Audit Service is stopped, so nothing can consume the event
ok - JetStream acknowledged and stored the event
ok - The NATS pod was replaced by a new one
ok - The stream still holds the event after the restart
ok - The Audit Service received the event, and made a record of it
ok - The same event sent again, past JetStream's duplicate check, created no second record
The consumer was deleted, so JetStream redelivers every stored request
ok - JetStream really redelivered: 4 deliveries, for 3 stored and 1 new
ok - All 2 of this tenant's records are unchanged, with no duplicates
ok - survives-: exactly one record
ok - after-repeat-: exactly one record
ok - The hash chain verifies across 3 records
ok - Nothing from this run reached the dead-letter stream
ok - The Audit Service did not crash
The Phase 2 exit gate passes.
```

| Part of the gate | What the check requires |
| --- | --- |
| An event survives a pod restart | With its only consumer stopped, the event exists only in JetStream. The NATS pod is deleted and replaced, and the stream still holds the event, which then becomes a record |
| A repeat creates one record | The same event, sent with a new message ID, gets past JetStream's duplicate check, so only the Audit Service can catch it |
| Real redelivery creates one record | The consumer is deleted while the service is stopped, so JetStream delivers every stored message again. Its own count must show this happened, and each event must still have one record |
| The record stays sound | The hash chain verifies, nothing reached the dead-letter stream, and the service did not crash |

## Every other check, on the same cluster

Each of these then ran straight after the gate, and passed:

| Command | Checks passed |
| --- | --- |
| `make phase-1-gate` | Passed, which shows Phase 2 did not break Phase 1 |
| `make postgres-check` | 53 |
| `make redis-check` | 21 |
| `make qdrant-check` | 9 |
| `make nats-check` | 21 |
| `make migrate-check` | 10 |
| `make outbox-check` | 14 |
| `make audit-check` | 20 |
| `make events-check` | 24 |

Whole cluster memory afterwards was 3.07 GiB of 10 GiB. Test records were removed, leaving 0 in the audit table.

## Problems the gate found

| Problem | Fix |
| --- | --- |
| Tempo was killed for running out of memory about every 30 minutes, while holding under 10 MiB of traces. Writing a block peaked above its 512 MiB limit, and its restart count had reached 17 | Write smaller blocks, every 5 minutes. It then ran for 37 minutes and 8 blocks with no restart, peaking at 405 MiB |
| Nothing flagged those restarts, so the crash loop went unnoticed through most of Phase 2 | Add `WindopsPodRestarting`, which fires at 3 restarts in 6 hours |
| The first version of this check could pass without any redelivery, if the new consumer had skipped old messages | Require JetStream's own delivery count to prove the redelivery |

Building Phase 2 also found these, each fixed in its own module's pull request:

| Module | Problem found on the cluster |
| --- | --- |
| M2.1 | The image's automatic `windops_admin` database was open to service logins |
| M2.2 | The Redis client sends `INCRBY`, which the login rules did not allow, so every rate limit would have failed |
| M2.6 | Asynchronous SQLAlchemy needed a dependency that was not declared |
| M2.7 | A dependency's outage would have sent good events to the dead-letter stream, and restarted the service |
| M2.8 | A new vulnerability in the base image's `apt`, and tests that failed at a second boundary |

## Limits

- The gate ran on a cluster with existing base images. It removed workloads, namespaces and data, but did not
  delete the Minikube cluster itself. M14.5 proves a bootstrap from a clean machine.
- JetStream runs one server, so this proves a pod restart, not a machine failure. It syncs to disk every 10
  seconds, so a sudden machine failure could lose the newest events that had not yet synced.
- Tempo's 405 MiB peak leaves about 100 MiB below its limit, and traffic is light. M13.5's load tests must
  measure it again.
- No other service sends audit requests yet, so the gate publishes them using Alarm's real login.
- CI has no cluster, so this check runs locally, and is not part of every pull request.
