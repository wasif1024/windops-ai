# windops-events

Publishes and consumes the durable events in SRS 7.6, through NATS JetStream.

| Rule | How it is enforced | Requirement |
| --- | --- | --- |
| Events use versioned Protobuf | Each subject accepts only its own message from `windops.events.v1` | EVT-001 |
| Every event carries a tenant | Publishing without one is refused, and a consumer dead-letters an event without one | DAT-007 |
| A retry does not store a duplicate | `event_id` is sent as `Nats-Msg-Id`, so JetStream drops a repeat within 2 minutes | EVT-003 |
| Failing messages cannot block a consumer | After `max_attempts`, a message moves to the dead-letter stream | EVT-003 |
| An outage does not spread | Publishing goes through a circuit breaker | SRS 7.7 |

## Publish

```python
from windops.events.v1 import events_pb2
from windops_events import EventPublisher, connect, events_breaker

client = await connect(url=url, username=username, password=password)
publisher = EventPublisher(client.jetstream(), producer="alarm", breaker=events_breaker())
await publisher.publish(
    "alarm.changed",
    tenant_id=identity.tenant_id,
    payload=events_pb2.AlarmChanged(alarm_id="a-1", turbine_id="t-1"),
)
```

`publish` raises `EventsUnavailableError` when JetStream cannot be reached. A service that must not lose
the event keeps it in its outbox and tries again (EVT-002, which M2.6 builds).

## Consume

```python
async def on_alarm(event: Event) -> None: ...  # Must be safe to run twice for the same event.id.


consumer = EventConsumer(js, subject="alarm.changed", durable="fault-detection", handler=on_alarm)
await consumer.start()
while running:
    await consumer.run_once()
```

Delivery is at least once. **A handler can see the same event twice**, so it must use `event.event_id` to
do its work only once (EVT-003).

| What happens | Result |
| --- | --- |
| The handler returns | The message is acknowledged |
| The handler raises | It is retried after a delay, up to `max_attempts` |
| It still fails | It moves to `deadletter.<durable>`, and an error is logged |
| The message is malformed, or has no valid tenant | It moves to the dead-letter stream at once, because a retry cannot help |
| The dead-letter stream cannot be reached | It stays in the original stream and is retried, so it is not lost |
