"""Events must carry a valid tenant and the right message, or they are refused."""

import pytest
from windops.events.v1 import events_pb2
from windops_events import (
    STREAMS,
    SUBJECTS,
    InvalidEventError,
    audit_request_subject,
    build_envelope,
    parse_event,
)

ALARM = events_pb2.AlarmChanged(alarm_id="a-1", turbine_id="t-1")


def envelope_bytes(**fields: str) -> bytes:
    envelope = events_pb2.EventEnvelope(
        event_id=fields.get("event_id", "e-1"),
        tenant_id=fields.get("tenant_id", "tenant-a"),
        producer=fields.get("producer", "alarm"),
    )
    envelope.alarm_changed.CopyFrom(ALARM)
    return envelope.SerializeToString()


def test_every_srs_subject_has_a_message() -> None:
    expected = {
        "telemetry.received",
        "telemetry.quality",
        "alarm.changed",
        "fault.detected",
        "case.updated",
        "evidence.changed",
        "workorder.submitted",
        "approval.decided",
        "workorder.completed",
        "audit.recorded",
    }
    assert set(SUBJECTS) == expected
    fields = events_pb2.EventEnvelope.DESCRIPTOR.oneofs_by_name["payload"].fields
    assert {field.name for field in fields} == {*SUBJECTS.values(), "audit_requested"}


def test_every_subject_is_in_exactly_one_stream() -> None:
    stored = [subject for spec in STREAMS for subject in spec.subjects if "." in subject]
    domain = [
        subject for subject in stored if not subject.startswith(("deadletter", "audit.requested"))
    ]
    assert sorted(domain) == sorted(SUBJECTS)


def test_telemetry_cannot_use_up_the_approval_streams_storage() -> None:
    by_subject = {subject: spec.name for spec in STREAMS for subject in spec.subjects}
    assert by_subject["telemetry.received"] != by_subject["approval.decided"]


def test_an_event_round_trips() -> None:
    envelope = build_envelope(
        "alarm.changed",
        tenant_id="tenant-a",
        payload=ALARM,
        producer="alarm",
        trace_parent="00-trace",
        request_id="r-1",
    )
    event = parse_event("alarm.changed", envelope.SerializeToString())
    assert event.tenant_id == "tenant-a"
    assert event.event_id == envelope.event_id
    assert event.producer == "alarm"
    assert event.payload == ALARM
    assert event.trace_parent == "00-trace"
    assert event.request_id == "r-1"
    assert event.occurred_at.tzinfo is not None


def test_a_given_event_id_is_kept_so_a_retry_is_recognized() -> None:
    envelope = build_envelope(
        "alarm.changed", tenant_id="tenant-a", payload=ALARM, producer="alarm", event_id="fixed"
    )
    assert envelope.event_id == "fixed"


def test_new_events_get_different_ids() -> None:
    ids = {
        build_envelope(
            "alarm.changed", tenant_id="tenant-a", payload=ALARM, producer="alarm"
        ).event_id
        for _ in range(3)
    }
    assert len(ids) == 3


@pytest.mark.parametrize("tenant", ["", "a:b", "a b", "a.b", "a*", "a>"])
def test_building_without_a_valid_tenant_is_refused(tenant: str) -> None:
    """DAT-007."""
    with pytest.raises(ValueError, match="tenant"):
        build_envelope("alarm.changed", tenant_id=tenant, payload=ALARM, producer="alarm")


def test_building_with_the_wrong_message_is_refused() -> None:
    wrong = events_pb2.CaseUpdated(case_id="c-1")
    with pytest.raises(ValueError, match="AlarmChanged"):
        build_envelope("alarm.changed", tenant_id="tenant-a", payload=wrong, producer="alarm")


def test_building_for_an_unknown_subject_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        build_envelope("alarm.deleted", tenant_id="tenant-a", payload=ALARM, producer="alarm")


def test_building_without_a_producer_is_refused() -> None:
    with pytest.raises(ValueError, match="producer"):
        build_envelope("alarm.changed", tenant_id="tenant-a", payload=ALARM, producer="")


@pytest.mark.parametrize("tenant", ["", "a:b", "a.b"])
def test_a_consumer_refuses_an_event_without_a_valid_tenant(tenant: str) -> None:
    """DAT-007."""
    with pytest.raises(InvalidEventError, match="tenant"):
        parse_event("alarm.changed", envelope_bytes(tenant_id=tenant))


def test_a_consumer_refuses_an_event_without_an_id() -> None:
    with pytest.raises(InvalidEventError, match="identifier"):
        parse_event("alarm.changed", envelope_bytes(event_id=""))


def test_a_consumer_refuses_an_event_without_a_producer() -> None:
    with pytest.raises(InvalidEventError, match="producer"):
        parse_event("alarm.changed", envelope_bytes(producer=""))


def test_a_consumer_refuses_the_wrong_message_for_the_subject() -> None:
    with pytest.raises(InvalidEventError, match=r"approval\.decided"):
        parse_event("approval.decided", envelope_bytes())


def test_a_consumer_refuses_bytes_that_are_not_an_event() -> None:
    with pytest.raises(InvalidEventError):
        parse_event("alarm.changed", b"\xff\xff not protobuf")


def test_a_consumer_refuses_an_unknown_subject() -> None:
    with pytest.raises(InvalidEventError, match="Unknown"):
        parse_event("alarm.deleted", envelope_bytes())


AUDIT = events_pb2.AuditRequested(action="role.changed", actor_id="u-1", outcome="succeeded")


def test_an_audit_request_uses_its_services_own_subject() -> None:
    subject = audit_request_subject("alarm")
    envelope = build_envelope(subject, tenant_id="tenant-a", payload=AUDIT, producer="alarm")
    event = parse_event(subject, envelope.SerializeToString())
    assert subject == "audit.requested.alarm"
    assert event.payload == AUDIT
    assert event.producer == "alarm"


def test_a_service_cannot_build_an_audit_request_in_anothers_name() -> None:
    with pytest.raises(ValueError, match="cannot ask"):
        build_envelope(
            "audit.requested.approval", tenant_id="tenant-a", payload=AUDIT, producer="alarm"
        )


def test_a_consumer_refuses_a_request_whose_producer_differs_from_its_subject() -> None:
    envelope = build_envelope(
        "audit.requested.alarm", tenant_id="tenant-a", payload=AUDIT, producer="alarm"
    )
    with pytest.raises(InvalidEventError, match="producer"):
        parse_event("audit.requested.approval", envelope.SerializeToString())


@pytest.mark.parametrize("subject", ["audit.requested.", "audit.requested.a.b"])
def test_a_malformed_audit_subject_is_refused(subject: str) -> None:
    with pytest.raises(ValueError, match=r"Unknown|cannot ask"):
        build_envelope(subject, tenant_id="tenant-a", payload=AUDIT, producer="alarm")


def test_audit_requests_have_their_own_stream() -> None:
    by_subject = {subject: spec for spec in STREAMS for subject in spec.subjects}
    assert by_subject["audit.requested.*"].name == "WINDOPS_AUDIT"
