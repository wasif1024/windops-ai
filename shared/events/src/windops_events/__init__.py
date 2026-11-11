"""Durable, tenant-carrying events, on JetStream or Azure Service Bus (SRS 7.6, ADR 0006)."""

from windops_events.backend import (
    BACKENDS,
    Consumer,
    Events,
    Publisher,
    events_backend,
    open_events,
)
from windops_events.client import (
    EventConsumer,
    EventPublisher,
    EventsUnavailableError,
    PublishedEvent,
    RetryLaterError,
    connect,
    ensure_streams,
    events_breaker,
    tls_context,
)
from windops_events.envelope import Event, InvalidEventError, build_envelope, parse_event
from windops_events.streams import (
    AUDIT_REQUEST_SUBJECTS,
    DEAD_LETTER_PREFIX,
    DEAD_LETTER_STREAM,
    STREAMS,
    SUBJECTS,
    StreamSpec,
    audit_request_subject,
)

__all__ = [
    "AUDIT_REQUEST_SUBJECTS",
    "BACKENDS",
    "DEAD_LETTER_PREFIX",
    "DEAD_LETTER_STREAM",
    "STREAMS",
    "SUBJECTS",
    "Consumer",
    "Event",
    "EventConsumer",
    "EventPublisher",
    "Events",
    "EventsUnavailableError",
    "InvalidEventError",
    "PublishedEvent",
    "Publisher",
    "RetryLaterError",
    "StreamSpec",
    "audit_request_subject",
    "build_envelope",
    "connect",
    "ensure_streams",
    "events_backend",
    "events_breaker",
    "open_events",
    "parse_event",
    "tls_context",
]
