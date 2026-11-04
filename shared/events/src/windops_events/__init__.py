"""Durable, tenant-carrying JetStream events (SRS 7.6, DAT-007)."""

from windops_events.client import (
    EventConsumer,
    EventPublisher,
    EventsUnavailableError,
    PublishedEvent,
    connect,
    ensure_streams,
    events_breaker,
)
from windops_events.envelope import Event, InvalidEventError, build_envelope, parse_event
from windops_events.streams import (
    DEAD_LETTER_PREFIX,
    DEAD_LETTER_STREAM,
    STREAMS,
    SUBJECTS,
    StreamSpec,
)

__all__ = [
    "DEAD_LETTER_PREFIX",
    "DEAD_LETTER_STREAM",
    "STREAMS",
    "SUBJECTS",
    "Event",
    "EventConsumer",
    "EventPublisher",
    "EventsUnavailableError",
    "InvalidEventError",
    "PublishedEvent",
    "StreamSpec",
    "build_envelope",
    "connect",
    "ensure_streams",
    "events_breaker",
    "parse_event",
]
