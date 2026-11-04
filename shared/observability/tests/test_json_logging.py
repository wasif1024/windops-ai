import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from windops_observability import ServiceInfo, configure_logging, request_context, tenant_context

SERVICE = ServiceInfo(name="telemetry", environment="test", version="1.2.3")


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    output = io.StringIO()
    configure_logging(SERVICE, stream=output)
    yield output
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def entries(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_required_fields_are_present(stream: io.StringIO) -> None:
    logging.getLogger("windops.test").warning("Started")
    entry = entries(stream)[0]
    assert entry["message"] == "Started"
    assert entry["severity"] == "WARNING"
    assert entry["service"] == "telemetry"
    assert entry["environment"] == "test"
    assert entry["version"] == "1.2.3"
    assert entry["logger"] == "windops.test"
    assert entry["time"].endswith("+00:00")


def test_each_record_is_one_line_of_json(stream: io.StringIO) -> None:
    logging.getLogger("windops.test").info("line one\nline two")
    assert len(stream.getvalue().splitlines()) == 1


def test_request_and_tenant_context_are_included(stream: io.StringIO) -> None:
    with request_context(request_id="req-1", tenant_id="tenant-a"):
        logging.getLogger("windops.test").info("Inside")
    logging.getLogger("windops.test").info("Outside")
    inside, outside = entries(stream)
    assert inside["request_id"] == "req-1"
    assert inside["tenant_id"] == "tenant-a"
    assert "request_id" not in outside
    assert "tenant_id" not in outside


def test_tenant_context_keeps_the_request_id_and_restores_the_tenant(
    stream: io.StringIO,
) -> None:
    with request_context(request_id="req-1", tenant_id="tenant-a"):
        with tenant_context("tenant-b"):
            logging.getLogger("windops.test").info("Inner")
        logging.getLogger("windops.test").info("Outer")
    inner, outer = entries(stream)
    assert inner["request_id"] == "req-1"
    assert inner["tenant_id"] == "tenant-b"
    assert outer["tenant_id"] == "tenant-a"


def test_trace_context_is_included(stream: io.StringIO) -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        logging.getLogger("windops.test").info("Traced")
        context = span.get_span_context()
    logging.getLogger("windops.test").info("Untraced")
    traced, untraced = entries(stream)
    assert traced["trace_id"] == format(context.trace_id, "032x")
    assert traced["span_id"] == format(context.span_id, "016x")
    assert "trace_id" not in untraced


def test_extra_fields_are_included_and_redacted(stream: io.StringIO) -> None:
    logging.getLogger("windops.test").info(
        "Called", extra={"dependency": "weather", "api_key": "abc", "note": "user@example.com"}
    )
    entry = entries(stream)[0]
    assert entry["dependency"] == "weather"
    assert entry["api_key"] == "[REDACTED]"
    assert entry["note"] == "[REDACTED]"


def test_extra_fields_cannot_replace_required_fields(stream: io.StringIO) -> None:
    logging.getLogger("windops.test").info(
        "Called", extra={"service": "fake", "severity": "fake", "tenant_id": "fake"}
    )
    entry = entries(stream)[0]
    assert entry["service"] == "telemetry"
    assert entry["severity"] == "INFO"


def test_message_is_redacted(stream: io.StringIO) -> None:
    logging.getLogger("windops.test").info("Header %s", "Bearer abc123")
    assert "abc123" not in stream.getvalue()


def test_exception_is_included_and_redacted(stream: io.StringIO) -> None:
    try:
        raise RuntimeError("Failed with password=hunter2")
    except RuntimeError:
        logging.getLogger("windops.test").exception("Failed")
    entry = entries(stream)[0]
    assert "RuntimeError" in entry["exception"]
    assert "hunter2" not in stream.getvalue()


def test_unserializable_values_become_redacted_text(stream: io.StringIO) -> None:
    class Custom:
        def __str__(self) -> str:
            return "token=abc123"

    logging.getLogger("windops.test").info("Called", extra={"value": Custom()})
    assert "abc123" not in stream.getvalue()


def test_breaker_state_change_logs_as_json(stream: io.StringIO) -> None:
    logging.getLogger("windops.resilience").warning(
        "Circuit breaker state changed",
        extra={"dependency": "weather", "from_state": "closed", "to_state": "open"},
    )
    entry = entries(stream)[0]
    assert entry["dependency"] == "weather"
    assert entry["to_state"] == "open"


def test_terminal_formatting_from_uvicorn_is_dropped(stream: io.StringIO) -> None:
    logging.getLogger("uvicorn").info("Started", extra={"color_message": "\x1b[36mStarted"})
    assert "color_message" not in entries(stream)[0]
