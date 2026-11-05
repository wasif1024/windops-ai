# Events

Durable facts move between services through NATS JetStream (SRS 7.6). This page covers what flows, how it
fails and how to see it. [windops-events](../shared/events/README.md) and
[windops-outbox](../shared/outbox/README.md) explain how to use them in code, and
[NATS JetStream](../infrastructure/data/nats/README.md) covers the server.

## Subjects

| Subject | Published by | Message | Stream |
| --- | --- | --- | --- |
| `telemetry.received` | Telemetry | `TelemetryReceived` | `WINDOPS_TELEMETRY` |
| `telemetry.quality` | Telemetry | `TelemetryQualityChanged` | `WINDOPS_TELEMETRY` |
| `alarm.changed` | Alarm | `AlarmChanged` | `WINDOPS_EVENTS` |
| `fault.detected` | Fault Detection | `FaultDetected` | `WINDOPS_EVENTS` |
| `case.updated` | Investigation | `CaseUpdated` | `WINDOPS_EVENTS` |
| `evidence.changed` | Asset, Knowledge, Inventory | `EvidenceChanged` | `WINDOPS_EVENTS` |
| `workorder.submitted` | Maintenance | `WorkOrderSubmitted` | `WINDOPS_EVENTS` |
| `approval.decided` | Approval | `ApprovalDecided` | `WINDOPS_EVENTS` |
| `workorder.completed` | Maintenance | `WorkOrderCompleted` | `WINDOPS_EVENTS` |
| `audit.recorded` | Audit | `AuditRecorded` | `WINDOPS_EVENTS` |
| `audit.requested.<service>` | That service only | `AuditRequested` | `WINDOPS_AUDIT` |
| `deadletter.<consumer>` | That consumer, on failure | The original message | `WINDOPS_DEAD_LETTER` |

[Service map](service-map.md) shows who listens for each. NATS permissions enforce who can publish, so a
service cannot forge another's event. Messages are defined in `shared/protobuf/windops/events/v1`, and every
one carries a tenant and an `event_id`.

## How an event travels

1. A service commits its state change and the event together, in its outbox (EVT-002).
2. Its relay publishes the event, and marks it sent once JetStream has stored it.
3. Each consumer receives it at least once, so it must use `event_id` to do its work only once (EVT-003).
4. A message that keeps failing, or is malformed, moves to the dead-letter stream.

## Dashboard and alerts

The **Events** dashboard in Grafana shows consumer lag, the outbox backlog, dead letters and stream storage.

| Alert | Fires when | What to do |
| --- | --- | --- |
| `WindopsDeadLetterMessages` | A message reached the dead-letter stream in the last hour | Run `make dead-letters`, then fix the cause below |
| `WindopsOutboxBacklog` | A service's oldest outbox event has waited 5 minutes, for 5 minutes | Check JetStream is up, and that the service's login can publish that subject |
| `WindopsOutboxRowsSetAside` | A service has an outbox row it cannot read | Find it with `failed_at`, in that service's database |
| `WindopsConsumerLag` | A consumer is over 1,000 messages behind, for 10 minutes | Check the consumer is running, and whether it needs more replicas |
| `WindopsDataStoreDown` | A data store has no ready replica, for 2 minutes | Check its pod and logs |

These go to Alertmanager. Nothing sends them to a person until M13.7 adds routing.

## Handle a dead letter

```bash
make dead-letters
```

This lists each message's consumer, subject, reason, error type and time, without its contents.

| Reason | Meaning | Action |
| --- | --- | --- |
| `invalid` | The message is malformed, has no valid tenant, or claims the wrong producer | Fix the publisher. A retry cannot help |
| `failed` | The handler kept raising an error, 5 times by default | Fix the handler using its logs, then have the publisher send the event again |

Dead letters are kept for 30 days. There is no replay command yet, so a resend is done by the service that
owns the data. Build one if real failures show it is needed.

## Limits

- `WindopsDeadLetterMessages` looks back one hour, so it stays active for up to an hour after the message.
- The relay reports its backlog every 15 seconds, and Prometheus scrapes every 30, so figures can lag by
  about 45 seconds.
- One event can be delivered more than once, and order across relays is not guaranteed.
