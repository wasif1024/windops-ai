"""The subjects in SRS 7.6, each event's message, and the streams that hold them."""

from dataclasses import dataclass

DAY_SECONDS = 24 * 60 * 60
MEBIBYTE = 1024 * 1024
DUPLICATE_WINDOW_SECONDS = 120

# Each subject, and the field in EventEnvelope that holds its message.
SUBJECTS = {
    "telemetry.received": "telemetry_received",
    "telemetry.quality": "telemetry_quality",
    "alarm.reported": "alarm_reported",
    "alarm.changed": "alarm_changed",
    "fault.detected": "fault_detected",
    "case.updated": "case_updated",
    "evidence.changed": "evidence_changed",
    "workorder.submitted": "work_order_submitted",
    "approval.decided": "approval_decided",
    "workorder.completed": "work_order_completed",
    "audit.recorded": "audit_recorded",
}
# Each service asks for records on its own subject, such as audit.requested.alarm, and may publish
# only there. So the subject proves which service is asking.
AUDIT_REQUEST_PREFIX = "audit.requested"
AUDIT_REQUEST_FIELD = "audit_requested"
AUDIT_REQUEST_SUBJECTS = f"{AUDIT_REQUEST_PREFIX}.*"
DEAD_LETTER_STREAM = "WINDOPS_DEAD_LETTER"
DEAD_LETTER_PREFIX = "deadletter"


def audit_request_subject(service: str) -> str:
    """Return the only subject a service may use to ask for an audit record."""
    return f"{AUDIT_REQUEST_PREFIX}.{service}"


@dataclass(frozen=True)
class StreamSpec:
    name: str
    subjects: tuple[str, ...]
    max_age_seconds: int
    max_bytes: int


# Telemetry is far busier than anything else, and has its own stream so its volume can never push
# an approval out of storage. PostgreSQL holds the source data, so a short life is safe.
_TELEMETRY_SUBJECTS = ("telemetry.received", "telemetry.quality")
STREAMS = (
    StreamSpec("WINDOPS_TELEMETRY", _TELEMETRY_SUBJECTS, DAY_SECONDS, 512 * MEBIBYTE),
    StreamSpec(
        "WINDOPS_EVENTS",
        tuple(subject for subject in SUBJECTS if subject not in _TELEMETRY_SUBJECTS),
        7 * DAY_SECONDS,
        512 * MEBIBYTE,
    ),
    # Separate and long-lived, because these must reach the authoritative record (EVT-004).
    StreamSpec("WINDOPS_AUDIT", (AUDIT_REQUEST_SUBJECTS,), 30 * DAY_SECONDS, 256 * MEBIBYTE),
    StreamSpec(DEAD_LETTER_STREAM, (f"{DEAD_LETTER_PREFIX}.>",), 30 * DAY_SECONDS, 128 * MEBIBYTE),
)
