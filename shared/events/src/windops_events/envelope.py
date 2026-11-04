"""Build and check event envelopes, so a bad event never looks like a good one."""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from google.protobuf.message import DecodeError, Message
from windops.events.v1 import events_pb2

from windops_events.streams import SUBJECTS

_TENANT = re.compile(r"[A-Za-z0-9_-]+")


class InvalidEventError(ValueError):
    """An event is malformed, or breaks a rule, so a retry cannot help."""


@dataclass(frozen=True)
class Event:
    subject: str
    event_id: str
    tenant_id: str
    occurred_at: datetime
    producer: str
    payload: Message
    trace_parent: str
    request_id: str


def build_envelope(
    subject: str,
    *,
    tenant_id: str,
    payload: Message,
    producer: str,
    event_id: str | None = None,
    trace_parent: str = "",
    request_id: str = "",
) -> events_pb2.EventEnvelope:
    """Return an envelope, refusing a missing tenant or the wrong message for the subject."""
    field = SUBJECTS.get(subject)
    if field is None:
        raise ValueError(f"Unknown event subject: {subject!r}")
    if not _TENANT.fullmatch(tenant_id):
        raise ValueError("An event needs a valid tenant identifier (DAT-007)")
    if not producer:
        raise ValueError("An event needs its producer")
    expected = events_pb2.EventEnvelope.DESCRIPTOR.fields_by_name[field].message_type
    if expected is None or expected != payload.DESCRIPTOR:
        name = expected.name if expected else "another message"
        raise ValueError(f"{subject} carries {name}, not {payload.DESCRIPTOR.name}")
    envelope = events_pb2.EventEnvelope(
        event_id=event_id or str(uuid.uuid4()),
        tenant_id=tenant_id,
        producer=producer,
        trace_parent=trace_parent,
        request_id=request_id,
    )
    envelope.occurred_at.FromDatetime(datetime.now(UTC))
    getattr(envelope, field).CopyFrom(payload)
    return envelope


def parse_event(subject: str, data: bytes) -> Event:
    """Return the event in a message, or raise InvalidEventError (DAT-007)."""
    field = SUBJECTS.get(subject)
    if field is None:
        raise InvalidEventError(f"Unknown event subject: {subject!r}")
    envelope = events_pb2.EventEnvelope()
    try:
        envelope.ParseFromString(data)
    except DecodeError as error:
        raise InvalidEventError("The message is not a valid event") from error
    if not _TENANT.fullmatch(envelope.tenant_id):
        raise InvalidEventError("The event has no valid tenant identifier")
    if not envelope.event_id:
        raise InvalidEventError("The event has no identifier")
    if not envelope.producer:
        raise InvalidEventError("The event has no producer")
    if envelope.WhichOneof("payload") != field:
        raise InvalidEventError(f"The event's message does not match {subject}")
    return Event(
        subject=subject,
        event_id=envelope.event_id,
        tenant_id=envelope.tenant_id,
        occurred_at=envelope.occurred_at.ToDatetime(tzinfo=UTC),
        producer=envelope.producer,
        payload=getattr(envelope, str(field)),
        trace_parent=envelope.trace_parent,
        request_id=envelope.request_id,
    )
