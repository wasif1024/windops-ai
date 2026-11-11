"""Both events back ends pass the same contract, and the settings choose between them (ADR 0006)."""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from azure.servicebus import ServiceBusMessage
from azure.servicebus.exceptions import ServiceBusConnectionError
from windops.events.v1 import events_pb2
from windops_events import (
    EventConsumer,
    EventPublisher,
    EventsUnavailableError,
    events_backend,
    events_breaker,
    open_events,
)
from windops_events.contract import SCENARIOS, Harness, run_contract
from windops_events.servicebus import (
    SUBSCRIPTIONS,
    ServiceBusConsumer,
    ServiceBusPublisher,
    _matches,
    filter_for,
    topic_for,
    topics,
)
from windops_resilience import BreakerState

# --- the switch


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, "nats"),
        ({"WINDOPS_ENVIRONMENT": "local"}, "nats"),
        ({"WINDOPS_ENVIRONMENT": "ci"}, "nats"),
        ({"WINDOPS_ENVIRONMENT": "dev"}, "servicebus"),
        ({"WINDOPS_ENVIRONMENT": "live"}, "servicebus"),
        ({"WINDOPS_ENVIRONMENT": "live", "WINDOPS_EVENTS_BACKEND": "auto"}, "servicebus"),
        ({"WINDOPS_ENVIRONMENT": "local", "WINDOPS_EVENTS_BACKEND": "servicebus"}, "servicebus"),
        ({"WINDOPS_ENVIRONMENT": "live", "WINDOPS_EVENTS_BACKEND": "NATS"}, "nats"),
    ],
)
def test_the_environment_chooses_the_back_end_unless_overridden(
    settings: dict[str, str], expected: str
) -> None:
    assert events_backend(settings) == expected


def test_an_unknown_back_end_is_refused() -> None:
    with pytest.raises(ValueError, match="WINDOPS_EVENTS_BACKEND"):
        events_backend({"WINDOPS_EVENTS_BACKEND": "kafka"})


def test_service_bus_needs_a_namespace_host_name() -> None:
    settings = {"WINDOPS_ENVIRONMENT": "dev", "WINDOPS_SERVICEBUS_NAMESPACE": "windops-dev"}
    with pytest.raises(ValueError, match=r"servicebus\.windows\.net"):
        asyncio.run(open_events("alarm", environ=settings))


# --- the mapping


def test_each_stream_is_a_topic_and_each_subject_finds_its_own() -> None:
    assert [name for name, _ in topics()] == [
        "windops-telemetry",
        "windops-events",
        "windops-audit",
    ]
    assert topic_for("telemetry.received") == "windops-telemetry"
    assert topic_for("alarm.changed") == "windops-events"
    assert topic_for("audit.requested.alarm") == "windops-audit"
    assert topic_for("audit.requested.*") == "windops-audit"
    with pytest.raises(ValueError, match="No topic"):
        topic_for("nothing.here")


def test_a_subscription_keeps_only_its_consumers_subjects() -> None:
    assert filter_for("alarm.changed") == "sys.Label = 'alarm.changed'"
    assert filter_for("audit.requested.*") == "sys.Label LIKE 'audit.requested.%'"
    assert [(s.topic, s.durable) for s in SUBSCRIPTIONS] == [("windops-audit", "audit")]
    assert _matches("audit.requested.*", "audit.requested.alarm")
    assert not _matches("audit.requested.*", "audit.requested.alarm.extra")
    assert _matches("deadletter.>", "deadletter.audit.x")


# --- stand-ins, each modelling what the contract relies on


@dataclass
class Stored:
    order: int
    subject: str
    data: bytes
    deliveries: int = 0
    done: bool = False
    in_flight: bool = False


class FakeJetStream:
    """JetStream as the contract sees it: deduplication, redelivery after nak, and dead letters."""

    def __init__(self) -> None:
        self.stored: list[Stored] = []
        self.ids: set[str] = set()
        self.dead: list[tuple[str, str]] = []

    async def publish(
        self, subject: str, payload: bytes, timeout: float, headers: dict[str, str]
    ) -> SimpleNamespace:
        if subject.startswith("deadletter."):
            self.dead.append((headers["Windops-Original-Subject"], headers["Windops-Reason"]))
            return SimpleNamespace(
                stream="WINDOPS_DEAD_LETTER", seq=len(self.dead), duplicate=False
            )
        message_id = headers["Nats-Msg-Id"]
        if message_id in self.ids:
            return SimpleNamespace(stream="WINDOPS_AUDIT", seq=0, duplicate=True)
        self.ids.add(message_id)
        self.stored.append(Stored(len(self.stored) + 1, subject, payload))
        return SimpleNamespace(stream="WINDOPS_AUDIT", seq=len(self.stored), duplicate=False)

    async def pull_subscribe(self, subject: str, durable: str, config: Any) -> "FakePull":
        return FakePull(self, subject)


class FakeMsg:
    def __init__(self, stored: Stored) -> None:
        self._stored = stored
        self.subject, self.data = stored.subject, stored.data
        self.metadata = SimpleNamespace(
            num_delivered=stored.deliveries, sequence=SimpleNamespace(stream=stored.order)
        )

    async def ack(self) -> None:
        self._stored.done, self._stored.in_flight = True, False

    async def term(self) -> None:
        await self.ack()

    async def nak(self, delay: float = 0) -> None:
        self._stored.in_flight = False


class FakePull:
    def __init__(self, js: FakeJetStream, subject: str) -> None:
        self._js, self._subject = js, subject

    async def fetch(self, batch: int, timeout: float) -> list[FakeMsg]:
        ready = [s for s in self._js.stored if not s.done and not s.in_flight
                 and _matches(self._subject, s.subject)][:batch]  # fmt: skip
        if not ready:
            raise TimeoutError
        for stored in ready:
            stored.deliveries += 1
            stored.in_flight = True
        return [FakeMsg(stored) for stored in ready]


@dataclass
class FakeServiceBus:
    """Service Bus as the contract sees it: deduplication, locks and a dead-letter queue."""

    topics: dict[str, list[Stored]] = field(default_factory=dict)
    ids: set[tuple[str, str]] = field(default_factory=set)
    dead: list[tuple[str, str]] = field(default_factory=list)
    broken: bool = False

    def get_topic_sender(self, topic: str) -> "FakeSender":
        return FakeSender(self, topic)

    def get_subscription_receiver(self, topic: str, subscription: str, **_: Any) -> "FakeReceiver":
        return FakeReceiver(self, topic)


@dataclass
class FakeSender:
    bus: FakeServiceBus
    topic: str

    async def send_messages(self, message: ServiceBusMessage) -> None:
        if self.bus.broken:
            raise ServiceBusConnectionError(message="down")
        key = (self.topic, str(message.message_id))
        if key in self.bus.ids:
            return
        self.bus.ids.add(key)
        stored = self.bus.topics.setdefault(self.topic, [])
        stored.append(Stored(len(stored) + 1, str(message.subject), b"".join(message.body)))

    async def close(self) -> None:
        return None


class FakeReceived:
    def __init__(self, stored: Stored) -> None:
        self.stored = stored
        self.subject = stored.subject
        self.body = iter([stored.data])
        # As real Service Bus counts it, and as it showed live: 0 on the first delivery.
        self.delivery_count = stored.deliveries - 1


@dataclass
class FakeReceiver:
    bus: FakeServiceBus
    topic: str
    opened: bool = False

    async def __aenter__(self) -> "FakeReceiver":
        self.opened = True
        return self

    async def receive_messages(self, max_message_count: int, max_wait_time: float) -> list[Any]:
        # A message left locked, as RetryLater leaves it, comes back here, as when its lock ends.
        ready = [s for s in self.bus.topics.get(self.topic, []) if not s.done][:max_message_count]
        for stored in ready:
            stored.deliveries += 1
        return [FakeReceived(stored) for stored in ready]

    async def complete_message(self, message: FakeReceived) -> None:
        message.stored.done = True

    async def abandon_message(self, message: FakeReceived) -> None:
        return None

    async def dead_letter_message(
        self, message: FakeReceived, reason: str, error_description: str
    ) -> None:
        message.stored.done = True
        self.bus.dead.append((message.subject, reason))

    async def close(self) -> None:
        return None


def nats_harness() -> Harness:
    js = FakeJetStream()

    async def raw(subject: str, data: bytes) -> None:
        await js.publish(subject, data, 1.0, {"Nats-Msg-Id": uuid.uuid4().hex})

    async def dead(subject: str) -> list[str]:
        return [reason for original, reason in js.dead if original == subject]

    return Harness(
        lambda producer: EventPublisher(js, producer=producer, breaker=events_breaker()),  # type: ignore[arg-type]
        lambda subject, durable, handler, attempts: EventConsumer(
            js, subject=subject, durable=durable, handler=handler, max_attempts=attempts,  # type: ignore[arg-type]
        ),
        "contract", raw, dead, patience_seconds=0.1,
    )  # fmt: skip


def service_bus_harness(bus: FakeServiceBus | None = None) -> Harness:
    bus = bus or FakeServiceBus()

    async def raw(subject: str, data: bytes) -> None:
        sender = bus.get_topic_sender(topic_for(subject))
        await sender.send_messages(
            ServiceBusMessage(data, subject=subject, message_id=uuid.uuid4().hex)
        )

    async def dead(subject: str) -> list[str]:
        return [reason for original, reason in bus.dead if original == subject]

    return Harness(
        lambda producer: ServiceBusPublisher(bus, producer=producer, breaker=events_breaker()),
        lambda subject, durable, handler, attempts: ServiceBusConsumer(
            bus, subject=subject, durable=durable, handler=handler, max_attempts=attempts,
        ),
        "contract", raw, dead, patience_seconds=0.1,
    )  # fmt: skip


HARNESSES: dict[str, Callable[[], Harness]] = {
    "nats": nats_harness,
    "servicebus": service_bus_harness,
}


@pytest.mark.parametrize("backend", list(HARNESSES))
@pytest.mark.parametrize(("name", "scenario"), SCENARIOS, ids=[n for n, _ in SCENARIOS])
def test_each_back_end_passes_each_scenario(backend: str, name: str, scenario: Any) -> None:
    asyncio.run(scenario(HARNESSES[backend](), uuid.uuid4().hex[:8]))


@pytest.mark.parametrize("backend", list(HARNESSES))
def test_each_back_end_passes_the_whole_contract_in_one_run(backend: str) -> None:
    passed: list[str] = []
    asyncio.run(run_contract(HARNESSES[backend](), passed.append))
    assert passed == [name for name, _ in SCENARIOS]


def test_a_service_bus_outage_is_reported_so_the_outbox_keeps_the_event() -> None:
    bus = FakeServiceBus(broken=True)
    breaker = events_breaker()
    publisher = ServiceBusPublisher(bus, producer="alarm", breaker=breaker)
    payload = events_pb2.AlarmChanged(alarm_id="a-1", turbine_id="t-1")

    async def scenario() -> None:
        for _ in range(12):
            with pytest.raises(EventsUnavailableError):
                await publisher.publish("alarm.changed", tenant_id="north-sea", payload=payload)

    asyncio.run(scenario())
    assert breaker.state is BreakerState.OPEN


def test_a_service_bus_message_carries_the_event_id_subject_and_tenant() -> None:
    bus = FakeServiceBus()
    sent: list[ServiceBusMessage] = []

    class Recording(FakeSender):
        async def send_messages(self, message: ServiceBusMessage) -> None:
            sent.append(message)

    bus.get_topic_sender = lambda topic: Recording(bus, topic)  # type: ignore[method-assign]
    publisher = ServiceBusPublisher(bus, producer="alarm", breaker=events_breaker())
    payload = events_pb2.AlarmChanged(alarm_id="a-1", turbine_id="t-1")
    published = asyncio.run(
        publisher.publish("alarm.changed", tenant_id="north-sea", payload=payload)
    )
    (message,) = sent
    assert message.message_id == published.event_id
    assert (message.subject, published.stream) == ("alarm.changed", "windops-events")
    assert message.application_properties == {b"tenant_id": "north-sea", b"producer": "alarm"} or (
        message.application_properties == {"tenant_id": "north-sea", "producer": "alarm"}
    )


def test_start_opens_the_receivers_link_before_any_receive() -> None:
    """A link left to open inside the first short receive timed out on every round, live."""
    bus = FakeServiceBus()
    consumer = ServiceBusConsumer(bus, subject="audit.requested.*", durable="audit",
                                  handler=lambda event: asyncio.sleep(0))  # fmt: skip

    async def scenario() -> FakeReceiver:
        await consumer.start()
        receiver: FakeReceiver = consumer._receiver
        return receiver

    assert asyncio.run(scenario()).opened is True


def test_a_service_bus_outage_while_receiving_is_reported() -> None:
    bus = FakeServiceBus()
    consumer = ServiceBusConsumer(bus, subject="audit.requested.*", durable="audit",
                                  handler=lambda event: asyncio.sleep(0))  # fmt: skip

    async def down(**_: Any) -> list[Any]:
        raise ServiceBusConnectionError(message="down")

    async def scenario() -> None:
        await consumer.start()
        consumer._receiver.receive_messages = down
        with pytest.raises(EventsUnavailableError):
            await consumer.run_once()

    asyncio.run(scenario())
