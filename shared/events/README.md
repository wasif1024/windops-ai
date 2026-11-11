# windops-events

Publishes and consumes the durable events in SRS 7.6, through NATS JetStream or Azure Service Bus
([ADR 0006](../../docs/adr/0006-azure-aligned-platform-services.md)). A service's code is the same on both.

| Rule | How it is enforced | Requirement |
| --- | --- | --- |
| Events use versioned Protobuf | Each subject accepts only its own message from `windops.events.v1` | EVT-001 |
| Every event carries a tenant | Publishing without one is refused, and a consumer dead-letters an event without one | DAT-007 |
| A retry does not store a duplicate | `event_id` is the message's ID, so JetStream and Service Bus each drop a repeat within 2 minutes | EVT-003 |
| Failing messages cannot block a consumer | After `max_attempts`, a message is dead-lettered | EVT-003 |
| An outage does not spread | Publishing goes through a circuit breaker | SRS 7.7 |

## Which back end

`open_events()` connects to the back end the settings choose:

| `WINDOPS_ENVIRONMENT` | Events | Sign-in |
| --- | --- | --- |
| `local`, `ci`, `test` | NATS JetStream, in the cluster | Its own login, from Kubernetes Secrets |
| `dev`, `staging`, `live` | Azure Service Bus, at `WINDOPS_SERVICEBUS_NAMESPACE` | No stored secret: Workload ID on AKS, the Azure CLI's session on a developer's machine |

`WINDOPS_EVENTS_BACKEND` overrides the choice when it is `nats` or `servicebus`. `auto`, the default, follows
`WINDOPS_ENVIRONMENT`.

| JetStream | Service Bus |
| --- | --- |
| Stream `WINDOPS_EVENTS`, with its subjects | Topic `windops-events` |
| Durable consumer | A subscription, made by `make servicebus-provision`, keeping only its subjects |
| `Nats-Msg-Id` and a 2-minute duplicate window | The message ID, and the same window on the topic |
| Stream `WINDOPS_DEAD_LETTER` | Each subscription's own dead-letter queue, with the reason and the error's type |

## Publish

```python
from windops.events.v1 import events_pb2
from windops_events import open_events

events = await open_events("alarm")
await events.publisher.publish(
    "alarm.changed",
    tenant_id=identity.tenant_id,
    payload=events_pb2.AlarmChanged(alarm_id="a-1", turbine_id="t-1"),
)
await events.close()
```

`publish` raises `EventsUnavailableError` when the back end cannot be reached. A service that must not lose
the event keeps it in its outbox and tries again (EVT-002).

## Consume

```python
async def on_alarm(event: Event) -> None: ...  # Must be safe to run twice for the same event.id.


consumer = events.consumer(subject="alarm.changed", durable="fault-detection", handler=on_alarm)
await consumer.start()
while running:
    await consumer.run_once()
```

On Service Bus, a new consumer also adds its subscription to `SUBSCRIPTIONS` in `servicebus.py`, so
provisioning creates it, as a consumer may not manage topics.

Delivery is at least once. **A handler can see the same event twice**, so it must use `event.event_id` to
do its work only once (EVT-003).

| What happens | Result |
| --- | --- |
| The handler returns | The message is acknowledged |
| The handler raises | It is retried, up to `max_attempts` |
| It still fails | It is dead-lettered, as `failed`, and an error is logged |
| The message is malformed, or has no valid tenant | It is dead-lettered at once, as `invalid`, because a retry cannot help |
| The handler raises `RetryLaterError`, as its own dependency is down | It comes back later and is never dead-lettered: after a delay on JetStream, and when its lock ends on Service Bus |
| The dead-letter stream cannot be reached | It stays where it was and is retried, so it is not lost |

## The same contract on both

`windops_events.contract` holds five scenarios every back end must pass: delivered once, a duplicate delivered
once, a failing handler dead-lettered, an invalid message dead-lettered at once, and `RetryLaterError` never
dead-lettered. The tests run them against stand-ins for both back ends, and `make servicebus-check` runs them
against a real namespace. On 2026-11-11 a real Standard namespace passed all five, and the run found three
things the stand-ins could not:
- **The transport:** the Azure SDKs' async transport needs `aiohttp`.
- **The receiver link:** it must be opened in full before the first receive.
- **Delivery counts:** Service Bus counts deliveries from 0.
