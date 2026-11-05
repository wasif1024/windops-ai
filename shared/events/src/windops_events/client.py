"""Publish and consume events, with the outage and failure behavior in SRS 7.6 and 7.7."""

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import nats
from google.protobuf.message import Message
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import APIError, BadRequestError, ServiceUnavailableError
from opentelemetry import metrics
from windops.events.v1 import events_pb2
from windops_resilience import BreakerConfig, BreakerOpenError, CircuitBreaker

from windops_events.envelope import Event, InvalidEventError, build_envelope, parse_event
from windops_events.streams import (
    DEAD_LETTER_PREFIX,
    DUPLICATE_WINDOW_SECONDS,
    STREAMS,
    StreamSpec,
)

logger = logging.getLogger(__name__)

SLOW_CALL_SECONDS = 2.0
PUBLISH_TIMEOUT_SECONDS = 2.0
Handler = Callable[[Event], Awaitable[None]]


class RetryLaterError(Exception):
    """A handler's own dependency is down. The event is fine, so this never dead-letters it."""


class EventsUnavailableError(ConnectionError):
    """JetStream cannot take the event. A ConnectionError, so it counts against the breaker."""


@dataclass(frozen=True)
class PublishedEvent:
    event_id: str
    stream: str
    sequence: int
    duplicate: bool


def events_breaker() -> CircuitBreaker:
    """Return a calling service's breaker for JetStream, with the SRS 7.7 defaults."""
    return CircuitBreaker("jetstream", BreakerConfig(slow_call_seconds=SLOW_CALL_SECONDS))


async def connect(
    *, url: str, username: str, password: str, name: str = "windops", ca_file: str | None = None
) -> Client:
    """Return a connection that keeps reconnecting, so a restart of NATS is not fatal.

    NATS accepts only TLS, so `ca_file` must name the CA unless this is a test's own server.
    """

    async def log_error(error: Exception) -> None:
        # A refused permission arrives here, not at the call that caused it.
        logger.error("NATS connection error: %s", type(error).__name__)

    return await nats.connect(
        servers=[url],
        tls=ssl.create_default_context(cafile=ca_file) if ca_file else None,
        user=username,
        password=password,
        name=name,
        connect_timeout=2,
        max_reconnect_attempts=-1,
        reconnect_time_wait=1,
        error_cb=log_error,
    )


async def ensure_streams(js: JetStreamContext, specs: tuple[StreamSpec, ...] = STREAMS) -> None:
    """Create each stream, or update it to match. Needs administrator rights."""
    for spec in specs:
        config = StreamConfig(
            name=spec.name,
            subjects=list(spec.subjects),
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_age=spec.max_age_seconds,
            max_bytes=spec.max_bytes,
            duplicate_window=DUPLICATE_WINDOW_SECONDS,
            num_replicas=1,
        )
        try:
            await js.add_stream(config)
        except BadRequestError:
            await js.update_stream(config)


class EventPublisher:
    """Publishes one service's events. Each publish waits until JetStream has stored the event."""

    def __init__(self, js: JetStreamContext, *, producer: str, breaker: CircuitBreaker) -> None:
        self._js = js
        self._producer = producer
        self._breaker = breaker

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
        """Store an event, or raise EventsUnavailableError so the caller can keep it."""
        envelope = build_envelope(
            subject,
            tenant_id=tenant_id,
            payload=payload,
            producer=self._producer,
            event_id=event_id,
            trace_parent=trace_parent,
            request_id=request_id,
        )
        return await self.publish_envelope(subject, envelope)

    async def publish_envelope(
        self, subject: str, envelope: events_pb2.EventEnvelope
    ) -> PublishedEvent:
        """Store an event that was already built, as the outbox does, keeping its ID and time."""

        async def send() -> PublishedEvent:
            try:
                ack = await self._js.publish(
                    subject,
                    envelope.SerializeToString(),
                    timeout=PUBLISH_TIMEOUT_SECONDS,
                    headers={"Nats-Msg-Id": envelope.event_id},
                )
            except (NatsError, ServiceUnavailableError, TimeoutError, OSError) as error:
                # NATS's own errors are not ConnectionError, so the breaker would miss an outage.
                raise EventsUnavailableError(type(error).__name__) from error
            return PublishedEvent(envelope.event_id, ack.stream, ack.seq or 0, bool(ack.duplicate))

        try:
            return await self._breaker.call(send)
        except BreakerOpenError as error:
            raise EventsUnavailableError("The JetStream breaker is open") from error


class EventConsumer:
    """Delivers one subject to a handler, at least once, and moves failures aside (EVT-003)."""

    def __init__(
        self,
        js: JetStreamContext,
        *,
        subject: str,
        durable: str,
        handler: Handler,
        max_attempts: int = 5,
        retry_delay_seconds: float = 5.0,
        ack_wait_seconds: float = 30.0,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("A consumer needs at least one attempt")
        self._js = js
        self._subject = subject
        self._durable = durable
        self._handler = handler
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay_seconds
        self._ack_wait = ack_wait_seconds
        self._subscription: JetStreamContext.PullSubscription | None = None
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("windops.events")
        self._dead_lettered = meter.create_counter(
            "windops.events.dead_lettered",
            description="Messages moved to the dead-letter stream",
        )

    async def start(self) -> None:
        """Create or join the durable consumer, which remembers its place across restarts."""
        self._subscription = await self._js.pull_subscribe(
            self._subject,
            durable=self._durable,
            config=ConsumerConfig(
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=self._ack_wait,
                # This class decides when to stop, so a message is never dropped without a copy.
                max_deliver=-1,
            ),
        )

    async def run_once(self, *, batch: int = 10, timeout: float = 1.0) -> int:
        """Handle the waiting messages, and return how many there were."""
        if self._subscription is None:
            raise RuntimeError("Call start() first")
        try:
            messages = await self._subscription.fetch(batch, timeout=timeout)
        except TimeoutError:
            return 0
        for message in messages:
            await self.handle(message)
        return len(messages)

    async def handle(self, message: Msg) -> None:
        try:
            event = parse_event(message.subject, message.data)
        except InvalidEventError as error:
            await self._dead_letter(message, "invalid", error)
            return
        try:
            await self._handler(event)
        except asyncio.CancelledError:
            raise
        except InvalidEventError as error:
            # The handler found what parsing could not, and a retry cannot help.
            await self._dead_letter(message, "invalid", error)
            return
        except RetryLaterError as error:
            logger.warning(
                "Event handler's dependency is down, so the event will wait",
                extra={"event_id": event.event_id, "error_type": type(error).__name__},
            )
            await message.nak(delay=self._retry_delay)
            return
        except Exception as error:
            if message.metadata.num_delivered >= self._max_attempts:
                await self._dead_letter(message, "failed", error)
            else:
                logger.warning(
                    "Event handler failed, and will retry",
                    extra={"event_id": event.event_id, "error_type": type(error).__name__},
                )
                await message.nak(delay=self._retry_delay)
            return
        await message.ack()

    async def _dead_letter(self, message: Msg, reason: str, error: Exception) -> None:
        sequence = message.metadata.sequence.stream if message.metadata.sequence else 0
        try:
            await self._js.publish(
                f"{DEAD_LETTER_PREFIX}.{self._durable}",
                message.data,
                timeout=PUBLISH_TIMEOUT_SECONDS,
                headers={
                    # Stable across retries, so a second attempt cannot store a second copy.
                    "Nats-Msg-Id": f"{self._durable}:{message.subject}:{sequence}",
                    "Windops-Original-Subject": message.subject,
                    "Windops-Consumer": self._durable,
                    "Windops-Reason": reason,
                    # The type only, because a message could hold tenant data (OBS-003).
                    "Windops-Error-Type": type(error).__name__,
                },
            )
        except (NatsError, APIError, TimeoutError, OSError):
            logger.exception("Could not dead-letter a message, so it stays for a retry")
            await message.nak(delay=self._retry_delay)
            return
        await message.term()
        self._dead_lettered.add(1, {"consumer": self._durable, "reason": reason})
        logger.error(
            "Message moved to the dead-letter stream",
            extra={
                "consumer": self._durable,
                "subject": message.subject,
                "reason": reason,
                "error_type": type(error).__name__,
            },
        )
