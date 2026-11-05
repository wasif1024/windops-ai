"""Durable, tenant-carrying JetStream events (SRS 7.6, DAT-007)."""

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
    "RetryLaterError",
    "StreamSpec",
    "audit_request_subject",
    "build_envelope",
    "connect",
    "ensure_streams",
    "events_breaker",
    "parse_event",
    "tls_context",
]
