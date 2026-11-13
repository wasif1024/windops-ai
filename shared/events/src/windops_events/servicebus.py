"""The same events on Azure Service Bus, for `dev`, `staging` and `live` (ADR 0006).

Each JetStream stream becomes a topic, with the same subjects, lifetime and two-minute duplicate
window, so the outbox can resend safely. Each consumer reads its own subscription, which keeps
only its subjects. A message's Service Bus subject is the event's subject, its message ID is the
event's ID, and its body is the same Protobuf envelope, so a handler sees no difference.

Failures keep the NATS behaviour (EVT-003):

- a message that cannot be read, or that a handler finds invalid, is dead-lettered at once;
- a handler that fails is tried again, and dead-lettered after `max_attempts`;
- a handler whose own dependency is down raises RetryLaterError: the message is left locked, and
  comes back when the lock ends, never dead-lettered.

Service Bus keeps each subscription's dead letters in its own dead-letter queue, with the reason
and the error's type, never its message, as that could hold tenant data (OBS-003).
"""

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from azure.core.exceptions import AzureError, ResourceExistsError
from azure.servicebus import ServiceBusMessage, ServiceBusReceivedMessage, ServiceBusReceiveMode
from azure.servicebus.exceptions import ServiceBusError
from azure.servicebus.management import SqlRuleFilter
from google.protobuf.message import Message
from opentelemetry import metrics
from windops.events.v1 import events_pb2
from windops_resilience import BreakerOpenError, CircuitBreaker

from windops_events.client import EventsUnavailableError, PublishedEvent, RetryLaterError
from windops_events.envelope import Event, InvalidEventError, build_envelope, parse_event
from windops_events.streams import (
    AUDIT_REQUEST_SUBJECTS,
    DEAD_LETTER_STREAM,
    DUPLICATE_WINDOW_SECONDS,
    STREAMS,
    StreamSpec,
)

logger = logging.getLogger(__name__)
Handler = Callable[[Event], Awaitable[None]]
CONTENT_TYPE = "application/x-protobuf"
# Service Bus's own limit, which our attempts stay well under, so we decide when to dead-letter.
SUBSCRIPTION_MAX_DELIVERIES = 100
LOCK_SECONDS = 30


def topic_for_stream(spec: StreamSpec) -> str:
    """WINDOPS_EVENTS becomes windops-events, as Service Bus names are lower case."""
    return spec.name.lower().replace("_", "-")


def _matches(pattern: str, subject: str) -> bool:
    """JetStream's wildcards, token by token: * is exactly one token, and > is one or more."""
    wanted, given = pattern.split("."), subject.split(".")
    for index, part in enumerate(wanted):
        if part == ">":
            return len(given) > index
        if index >= len(given) or (part != "*" and part != given[index]):
            return False
    return len(given) == len(wanted)


def topic_for(subject: str) -> str:
    """The topic that holds a subject, or a stream's own wildcard, as the JetStream stream does."""
    for spec in STREAMS:
        if spec.name == DEAD_LETTER_STREAM:
            continue
        if subject in spec.subjects or any(_matches(p, subject) for p in spec.subjects):
            return topic_for_stream(spec)
    raise ValueError(f"No topic holds {subject}")


def filter_for(subject: str) -> str:
    """The SQL filter that keeps one subject, or a wildcard's subjects, on a subscription."""
    if subject.endswith((".*", ".>")):
        return f"sys.Label LIKE '{subject[:-1]}%'"
    return f"sys.Label = '{subject}'"


@dataclass(frozen=True)
class SubscriptionSpec:
    """One consumer's subscription, created ahead of time, as consumers may not manage topics."""

    subject: str
    durable: str

    @property
    def topic(self) -> str:
        return topic_for(self.subject)


# Every consumer today. A new consumer adds its subscription here, and provisioning creates it.
SUBSCRIPTIONS = (
    SubscriptionSpec(AUDIT_REQUEST_SUBJECTS, "audit"),
    SubscriptionSpec("alarm.reported", "alarm"),
    SubscriptionSpec("telemetry.received", "fault-detection"),
    SubscriptionSpec("fault.detected", "investigation"),
)


def topics() -> tuple[tuple[str, StreamSpec], ...]:
    """The topics, with the stream each stands for. Dead letters need no topic of their own."""
    return tuple(
        (topic_for_stream(spec), spec) for spec in STREAMS if spec.name != DEAD_LETTER_STREAM
    )


async def ensure_topology(
    admin: Any, subscriptions: tuple[SubscriptionSpec, ...] = SUBSCRIPTIONS
) -> None:
    """Create each topic and subscription, or leave it. Needs the Data Owner role."""
    for name, spec in topics():
        with contextlib.suppress(ResourceExistsError):
            await admin.create_topic(
                name,
                requires_duplicate_detection=True,
                duplicate_detection_history_time_window=timedelta(seconds=DUPLICATE_WINDOW_SECONDS),
                default_message_time_to_live=timedelta(seconds=spec.max_age_seconds),
                max_size_in_megabytes=1024,
            )
    for subscription in subscriptions:
        try:
            await admin.create_subscription(
                subscription.topic,
                subscription.durable,
                max_delivery_count=SUBSCRIPTION_MAX_DELIVERIES,
                lock_duration=timedelta(seconds=LOCK_SECONDS),
                dead_lettering_on_message_expiration=True,
            )
            # Replace the catch-all rule with one that keeps only this consumer's subjects.
            await admin.delete_rule(subscription.topic, subscription.durable, "$Default")
            await admin.create_rule(
                subscription.topic, subscription.durable, "subjects",
                filter=SqlRuleFilter(filter_for(subscription.subject)),
            )  # fmt: skip
        except ResourceExistsError:
            pass


def _unavailable(error: Exception) -> Exception:
    return EventsUnavailableError(type(error).__name__)


class ServiceBusPublisher:
    """Publishes one service's events. Each publish waits until Service Bus has stored the event."""

    def __init__(self, client: Any, *, producer: str, breaker: CircuitBreaker) -> None:
        self._client = client
        self._producer = producer
        self._breaker = breaker
        self._senders: dict[str, Any] = {}

    async def publish(
        self,
        subject: str,
        *,
        tenant_id: str,
        payload: Message,
        event_id: str | None = None,
        trace_parent: str = "",
        request_id: str = "",
    ) -> PublishedEvent:
        envelope = build_envelope(
            subject, tenant_id=tenant_id, payload=payload, producer=self._producer,
            event_id=event_id, trace_parent=trace_parent, request_id=request_id,
        )  # fmt: skip
        return await self.publish_envelope(subject, envelope)

    async def publish_envelope(
        self, subject: str, envelope: events_pb2.EventEnvelope
    ) -> PublishedEvent:
        topic = topic_for(subject)

        async def send() -> PublishedEvent:
            try:
                sender = self._senders.get(topic)
                if sender is None:
                    sender = self._client.get_topic_sender(topic)
                    self._senders[topic] = sender
                await sender.send_messages(
                    ServiceBusMessage(
                        envelope.SerializeToString(),
                        message_id=envelope.event_id,
                        subject=subject,
                        content_type=CONTENT_TYPE,
                        application_properties={
                            "tenant_id": envelope.tenant_id, "producer": envelope.producer,
                        },
                    )
                )  # fmt: skip
            except (ServiceBusError, AzureError, TimeoutError, OSError) as error:
                raise _unavailable(error) from error
            # Service Bus drops a duplicate silently, so it cannot be reported.
            return PublishedEvent(envelope.event_id, topic, 0, False)

        try:
            return await self._breaker.call(send)
        except BreakerOpenError as error:
            raise EventsUnavailableError("The Service Bus breaker is open") from error

    async def close(self) -> None:
        for sender in self._senders.values():
            await sender.close()
        self._senders.clear()


def _body(message: ServiceBusReceivedMessage) -> bytes:
    return b"".join(bytes(section) for section in message.body)


class ServiceBusConsumer:
    """Delivers one subject to a handler, at least once, and moves failures aside (EVT-003)."""

    def __init__(
        self,
        client: Any,
        *,
        subject: str,
        durable: str,
        handler: Handler,
        max_attempts: int = 5,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("A consumer needs at least one attempt")
        self._client = client
        self._subscription = SubscriptionSpec(subject, durable)
        self._handler = handler
        self._max_attempts = max_attempts
        self._receiver: Any = None
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("windops.events")
        self._dead_lettered = meter.create_counter(
            "windops.events.dead_lettered", description="Messages moved to the dead-letter queue"
        )

    async def start(self) -> None:
        """Open the receiver's link. The subscription itself is made by provisioning, not here.

        The link opens here, in full, as `async with` would open it. Left to the first receive, it
        would have to open within that receive's short wait, and a link that is slow to open would
        time out on every round, never receiving at all.
        """
        try:
            receiver = self._client.get_subscription_receiver(
                self._subscription.topic, self._subscription.durable,
                receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            )  # fmt: skip
            self._receiver = await receiver.__aenter__()
        except (ServiceBusError, AzureError, OSError) as error:
            raise _unavailable(error) from error

    async def run_once(self, *, batch: int = 10, timeout: float = 1.0) -> int:
        if self._receiver is None:
            raise RuntimeError("Call start() first")
        try:
            messages = await self._receiver.receive_messages(
                max_message_count=batch, max_wait_time=timeout
            )
        except (ServiceBusError, AzureError, OSError) as error:
            raise _unavailable(error) from error
        for message in messages:
            await self.handle(message)
        return len(messages)

    async def handle(self, message: ServiceBusReceivedMessage) -> None:
        subject = str(message.subject or "")
        try:
            event = parse_event(subject, _body(message))
        except InvalidEventError as error:
            await self._dead_letter(message, "invalid", error)
            return
        try:
            await self._handler(event)
        except InvalidEventError as error:
            await self._dead_letter(message, "invalid", error)
            return
        except RetryLaterError as error:
            # Left locked: it comes back when the lock ends, so a down dependency is not hammered.
            logger.warning(
                "Event handler's dependency is down, so the event will wait",
                extra={"event_id": event.event_id, "error_type": type(error).__name__},
            )
            return
        except Exception as error:
            # Service Bus counts redeliveries: 0 on the first delivery, unlike JetStream's 1.
            if (message.delivery_count or 0) + 1 >= self._max_attempts:
                await self._dead_letter(message, "failed", error)
            else:
                logger.warning(
                    "Event handler failed, and will retry",
                    extra={"event_id": event.event_id, "error_type": type(error).__name__},
                )
                await self._settle(self._receiver.abandon_message, message)
            return
        await self._settle(self._receiver.complete_message, message)

    async def _settle(
        self, action: Callable[..., Awaitable[None]], message: Any, **kw: Any
    ) -> bool:
        try:
            await action(message, **kw)
        except (ServiceBusError, AzureError, OSError):
            # The lock ends, and the message comes back, so nothing is lost.
            logger.exception("Could not settle a message, so it will come back")
            return False
        return True

    async def _dead_letter(self, message: Any, reason: str, error: Exception) -> None:
        moved = await self._settle(
            self._receiver.dead_letter_message, message,
            reason=reason, error_description=type(error).__name__,
        )  # fmt: skip
        if not moved:
            return
        self._dead_lettered.add(1, {"consumer": self._subscription.durable, "reason": reason})
        logger.error(
            "Message moved to the dead-letter queue",
            extra={
                "consumer": self._subscription.durable,
                "subject": str(message.subject or ""),
                "reason": reason,
                "error_type": type(error).__name__,
            },
        )

    async def close(self) -> None:
        if self._receiver is not None:
            await self._receiver.close()
            self._receiver = None
