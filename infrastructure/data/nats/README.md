# NATS JetStream

`windops-nats` holds durable events and dead letters on a persistent volume, and is reachable only inside
the cluster. Services use [windops-events](../../../shared/events/README.md).

## Streams

| Stream | Subjects | Kept for | Size limit |
| --- | --- | --- | --- |
| `WINDOPS_TELEMETRY` | `telemetry.received`, `telemetry.quality` | 1 day | 512 MiB |
| `WINDOPS_EVENTS` | The other 8 subjects in SRS 7.6 | 7 days | 512 MiB |
| `WINDOPS_AUDIT` | `audit.requested.<service>` | 30 days | 256 MiB |
| `WINDOPS_DEAD_LETTER` | `deadletter.<consumer>` | 30 days | 128 MiB |

Telemetry has its own stream, so its volume cannot push an approval out of storage. When a stream reaches
a limit, its oldest messages are removed. JetStream is not a system of record (SRS 8.1).

## Service access

Each service has a login, in the Secret `windops-<service>-events` in its namespace, with the keys `url`,
`username` and `password`.

- A service can publish only the subjects it owns, as listed in `PUBLISHERS` in `scripts/nats_access.py`.
  Only Approval can publish `approval.decided`, for example.
- A service that stores data can ask for audit records only on `audit.requested.<its own name>`.
- Any service can create consumers and read events, because events hold identifiers, not evidence.
- Only the `windops_admin` login can create, change or delete a stream.

## Add a service or subject

1. Update `scripts/nats_access.py`, and `shared/events` for a new subject.
2. Run `make secrets`.
3. Run `kubectl --context windops --namespace windops-data rollout restart statefulset/windops-nats`.
   Stored events survive this.
4. Run `make infra`, which applies stream changes.

## Back up and restore

Streams are short-lived, and each service's outbox and database hold the source of each event. To keep
stream data before a risky change, copy the whole volume while NATS is stopped:

```bash
kubectl --context windops --namespace windops-data scale statefulset windops-nats --replicas=0
# Copy data-windops-nats-0 with your volume tooling, then:
kubectl --context windops --namespace windops-data scale statefulset windops-nats --replicas=1
```

This copy has not been tested, and DAT-003 does not require it. A tested restore is only required for
PostgreSQL and Qdrant.

## Reset

This **deletes all stored events and consumer positions**.

```bash
kubectl --context windops --namespace windops-data delete statefulset windops-nats
kubectl --context windops --namespace windops-data delete pvc data-windops-nats-0
make infra
```

## When JetStream is down

Publishing raises `EventsUnavailableError`, and the circuit breaker opens. State changes still commit, with
their events held in the outbox until it returns, once M2.6 builds that (SRS 7.7).

`make nats-check` proves durability, permissions, dead letters and an outage on the cluster.

## Limits

- Connections are not encrypted until M3.3 adds mTLS.
- Any login can subscribe to `_INBOX.>`, the reply subjects, so a service could read replies meant for
  another. Events hold identifiers, not evidence.
- Queued agent runs have no subject yet. M8.2 adds one, because SRS 7.6 lists only the initial subjects.
- One server means no failover. A production deployment would run three (ADR 0003 has the same reasoning
  for PostgreSQL).
