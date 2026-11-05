"""Publishing and consuming, including each failure path in EVT-003 and SRS 7.7."""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, cast

import pytest
from nats.aio.msg import Msg
from nats.errors import NoServersError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext
from nats.js.api import StorageType
from nats.js.errors import BadRequestError, ServiceUnavailableError
from windops.events.v1 import events_pb2
from windops_events import (
    Event,
    EventConsumer,
    EventPublisher,
    EventsUnavailableError,
    InvalidEventError,
    RetryLaterError,
    StreamSpec,
    build_envelope,
    ensure_streams,
    events_breaker,
)
from windops_resilience import BreakerState

ALARM = events_pb2.AlarmChanged(alarm_id="a-1", turbine_id="t-1")


def run_async[**P](test: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Run an async test, because the repository uses no async test plugin."""

    @functools.wraps(test)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


class FakeJetStream:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self.error: Exception | None = None
        self.duplicate = False

    async def publish(
        self, subject: str, payload: bytes, timeout: float, headers: dict[str, str]
    ) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        self.published.append((subject, payload, headers))
        return SimpleNamespace(stream="WINDOPS_EVENTS", seq=7, duplicate=self.duplicate)


class FakeMessage:
    def __init__(self, data: bytes, *, delivered: int = 1, subject: str = "alarm.changed") -> None:
        self.subject = subject
        self.data = data
        self.metadata = SimpleNamespace(
            num_delivered=delivered, sequence=SimpleNamespace(stream=42)
        )
        self.outcomes: list[str] = []
        self.delay: float | None = None

    async def ack(self) -> None:
        self.outcomes.append("ack")

    async def nak(self, delay: float) -> None:
        self.outcomes.append("nak")
        self.delay = delay

    async def term(self) -> None:
        self.outcomes.append("term")


def valid_event() -> bytes:
    return build_envelope(
        "alarm.changed", tenant_id="tenant-a", payload=ALARM, producer="alarm", event_id="e-1"
    ).SerializeToString()


def publisher_for(fake: FakeJetStream, breaker: Any = None) -> EventPublisher:
    return EventPublisher(
        cast(JetStreamContext, fake), producer="alarm", breaker=breaker or events_breaker()
    )


def consumer_for(fake: FakeJetStream, handler: Callable[[Event], Any]) -> EventConsumer:
    return EventConsumer(
        cast(JetStreamContext, fake),
        subject="alarm.changed",
        durable="fault-detection",
        handler=handler,
        max_attempts=3,
        retry_delay_seconds=9.0,
    )


async def succeed(event: Event) -> None:
    return None


async def fail(event: Event) -> None:
    raise RuntimeError("secret detail")


def as_msg(message: FakeMessage) -> Msg:
    return cast(Msg, message)


@run_async
async def test_publish_sends_the_event_id_for_deduplication() -> None:
    fake = FakeJetStream()
    result = await publisher_for(fake).publish(
        "alarm.changed", tenant_id="tenant-a", payload=ALARM, event_id="fixed"
    )
    assert fake.published[0][2]["Nats-Msg-Id"] == "fixed"
    assert (result.event_id, result.sequence, result.duplicate) == ("fixed", 7, False)


@run_async
async def test_publish_reports_a_duplicate() -> None:
    fake = FakeJetStream()
    fake.duplicate = True
    result = await publisher_for(fake).publish("alarm.changed", tenant_id="tenant-a", payload=ALARM)
    assert result.duplicate


@pytest.mark.parametrize(
    "error",
    [NoServersError(), NatsTimeoutError(), ServiceUnavailableError(), OSError("reset")],
)
@run_async
async def test_publish_reports_an_outage_so_the_caller_can_keep_the_event(
    error: Exception,
) -> None:
    """SRS 7.7: the event stays in the outbox until JetStream returns."""
    fake = FakeJetStream()
    fake.error = error
    with pytest.raises(EventsUnavailableError):
        await publisher_for(fake).publish("alarm.changed", tenant_id="tenant-a", payload=ALARM)


@run_async
async def test_nats_errors_open_the_breaker() -> None:
    """NATS's own errors must count, or an outage would never open the breaker."""
    fake = FakeJetStream()
    fake.error = NoServersError()
    breaker = events_breaker()
    publisher = publisher_for(fake, breaker)
    for _ in range(10):
        with pytest.raises(EventsUnavailableError):
            await publisher.publish("alarm.changed", tenant_id="tenant-a", payload=ALARM)
    assert breaker.state is BreakerState.OPEN
    fake.error = None
    with pytest.raises(EventsUnavailableError):
        await publisher.publish("alarm.changed", tenant_id="tenant-a", payload=ALARM)
    assert fake.published == []


@run_async
async def test_publish_without_a_tenant_never_reaches_jetstream() -> None:
    fake = FakeJetStream()
    with pytest.raises(ValueError, match="tenant"):
        await publisher_for(fake).publish("alarm.changed", tenant_id="", payload=ALARM)
    assert fake.published == []


@run_async
async def test_a_handled_message_is_acknowledged() -> None:
    message = FakeMessage(valid_event())
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    await consumer_for(FakeJetStream(), handler).handle(as_msg(message))
    assert message.outcomes == ["ack"]
    assert seen[0].event_id == "e-1"


@run_async
async def test_a_failing_handler_is_retried_with_a_delay() -> None:
    fake = FakeJetStream()
    message = FakeMessage(valid_event(), delivered=1)
    await consumer_for(fake, fail).handle(as_msg(message))
    assert message.outcomes == ["nak"]
    assert message.delay == 9.0
    assert fake.published == []


@run_async
async def test_repeated_failure_moves_the_message_to_the_dead_letter_stream() -> None:
    """EVT-003."""
    fake = FakeJetStream()
    data = valid_event()
    message = FakeMessage(data, delivered=3)
    await consumer_for(fake, fail).handle(as_msg(message))
    assert message.outcomes == ["term"]
    subject, stored, headers = fake.published[0]
    assert subject == "deadletter.fault-detection"
    assert stored == data
    assert headers["Windops-Original-Subject"] == "alarm.changed"
    assert headers["Windops-Consumer"] == "fault-detection"
    assert headers["Windops-Reason"] == "failed"
    assert headers["Windops-Error-Type"] == "RuntimeError"


@run_async
async def test_dead_letter_headers_hold_no_error_message() -> None:
    """An error message could hold tenant data, so only its type is kept (OBS-003)."""
    fake = FakeJetStream()
    await consumer_for(fake, fail).handle(as_msg(FakeMessage(valid_event(), delivered=3)))
    assert "secret detail" not in str(fake.published[0][2])


@run_async
async def test_a_second_dead_letter_attempt_uses_the_same_id() -> None:
    """A redelivery must not store a second copy."""
    fake = FakeJetStream()
    consumer = consumer_for(fake, fail)
    await consumer.handle(as_msg(FakeMessage(valid_event(), delivered=3)))
    await consumer.handle(as_msg(FakeMessage(valid_event(), delivered=4)))
    assert fake.published[0][2]["Nats-Msg-Id"] == fake.published[1][2]["Nats-Msg-Id"]


@pytest.mark.parametrize("data", [b"\xff\xff not protobuf", b""])
@run_async
async def test_an_invalid_message_is_dead_lettered_at_once(data: bytes) -> None:
    """A retry cannot fix it, and the handler must never see it."""
    fake = FakeJetStream()
    called = False

    async def handler(event: Event) -> None:
        nonlocal called
        called = True

    message = FakeMessage(data, delivered=1)
    await consumer_for(fake, handler).handle(as_msg(message))
    assert message.outcomes == ["term"]
    assert fake.published[0][2]["Windops-Reason"] == "invalid"
    assert not called


@run_async
async def test_an_event_without_a_tenant_is_dead_lettered() -> None:
    """DAT-007."""
    fake = FakeJetStream()
    envelope = events_pb2.EventEnvelope(event_id="e-1", producer="alarm")
    envelope.alarm_changed.CopyFrom(ALARM)
    message = FakeMessage(envelope.SerializeToString())
    await consumer_for(fake, succeed).handle(as_msg(message))
    assert message.outcomes == ["term"]


@run_async
async def test_a_message_is_kept_if_the_dead_letter_stream_cannot_be_reached() -> None:
    """Terminating here would lose it, so it stays in the original stream."""
    fake = FakeJetStream()
    fake.error = NoServersError()
    message = FakeMessage(valid_event(), delivered=3)
    await consumer_for(fake, fail).handle(as_msg(message))
    assert message.outcomes == ["nak"]


@run_async
async def test_a_cancelled_handler_does_not_acknowledge() -> None:
    message = FakeMessage(valid_event())

    async def handler(event: Event) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await consumer_for(FakeJetStream(), handler).handle(as_msg(message))
    assert message.outcomes == []


@run_async
async def test_run_once_needs_start() -> None:
    with pytest.raises(RuntimeError, match="start"):
        await consumer_for(FakeJetStream(), succeed).run_once()


def test_a_consumer_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="attempt"):
        EventConsumer(
            cast(JetStreamContext, FakeJetStream()),
            subject="alarm.changed",
            durable="x",
            handler=succeed,
            max_attempts=0,
        )


class FakeStreamAdmin:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.calls: list[tuple[str, Any]] = []

    async def add_stream(self, config: Any) -> None:
        self.calls.append(("add", config))
        if self.exists:
            raise BadRequestError()

    async def update_stream(self, config: Any) -> None:
        self.calls.append(("update", config))


SPEC = (StreamSpec("TEST", ("test.created",), 60, 1024),)


@run_async
async def test_a_new_stream_is_created_on_disk_with_its_limits() -> None:
    admin = FakeStreamAdmin(exists=False)
    await ensure_streams(cast(JetStreamContext, admin), SPEC)
    action, config = admin.calls[0]
    assert action == "add"
    assert (config.name, config.subjects) == ("TEST", ["test.created"])
    assert (config.max_age, config.max_bytes) == (60, 1024)
    assert config.storage == StorageType.FILE
    assert config.duplicate_window == 120


@run_async
async def test_an_existing_stream_is_updated_to_match() -> None:
    admin = FakeStreamAdmin(exists=True)
    await ensure_streams(cast(JetStreamContext, admin), SPEC)
    assert [action for action, _ in admin.calls] == ["add", "update"]


@run_async
async def test_a_dependency_outage_never_dead_letters_an_event() -> None:
    """The event is fine, so however long the outage lasts, it must wait."""
    fake = FakeJetStream()

    async def handler(event: Event) -> None:
        raise RetryLaterError("database down")

    message = FakeMessage(valid_event(), delivered=500)
    await consumer_for(fake, handler).handle(as_msg(message))
    assert message.outcomes == ["nak"]
    assert fake.published == []


@run_async
async def test_a_handler_can_reject_an_event_as_invalid_at_once() -> None:
    fake = FakeJetStream()

    async def handler(event: Event) -> None:
        raise InvalidEventError("missing a required field")

    message = FakeMessage(valid_event(), delivered=1)
    await consumer_for(fake, handler).handle(as_msg(message))
    assert message.outcomes == ["term"]
    assert fake.published[0][2]["Windops-Reason"] == "invalid"
