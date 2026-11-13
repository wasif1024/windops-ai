# ADR 0007: Alarms arrive through the device route, as events

**Status:** Accepted, 2026-11-13

## Context

FR-ALM-001 asks the Alarm Service to ingest alarm events from each turbine's controller. The SRS does not
say how they arrive. A device already has one way in: the Telemetry Service's device route, behind the
gateway, with a credential scoped to its tenant and turbine (FR-TEL-005). The service map gives the Alarm
Service no synchronous dependencies on the ingestion side.

There were three ways to bring alarms in:

1. A device route of the Alarm Service's own. It would need the device credentials, which the Telemetry
   Service owns in its own database.
2. The Telemetry Service calls the Alarm Service for each alarm. A device's alarms would then fail while
   the Alarm Service was down.
3. The Telemetry Service checks each alarm and queues it as an event, which the Alarm Service consumes.

## Decision

Option 3. A controller sends alarms to `/ingest/v1/alarms`, beside its samples. The Telemetry Service
checks their shape and that they are the device's own, then queues each as `alarm.reported` through its
outbox, in the same transaction (EVT-002). Its event ID comes from the device's report ID, so a resend is
one message. The Alarm Service consumes the subject idempotently, by report ID (EVT-003), and owns what
an alarm means: its state, its history and its severity.

`alarm.reported` is a subject the SRS 7.6 list did not have. Only the Telemetry Service may publish it,
and the Alarm Service rejects one from anyone else.

## Consequences

- One device credential, one route through the gateway, and one place devices are checked.
- An Alarm Service outage only delays alarms: they wait in the stream, and nothing a device sent is lost.
- Reports can arrive out of order, after an outage or a resend. The Alarm Service keeps a clear that comes
  before its raise, and applies it when the raise arrives.
- The Telemetry Service now publishes three subjects, and Azure needs a Service Bus subscription for the
  Alarm Service. Both are generated, and tests keep them matched.
