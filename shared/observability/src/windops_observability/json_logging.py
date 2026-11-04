"""Write structured JSON logs, with the fields required by OBS-001."""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

from opentelemetry import trace

from windops_observability.redaction import redact, redact_text
from windops_observability.service import ServiceInfo

_request_id: ContextVar[str | None] = ContextVar("windops_request_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("windops_tenant_id", default=None)

# Attributes every LogRecord has, so anything else was passed through `extra`.
_STANDARD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "color_message",  # Terminal formatting added by uvicorn.
}


@contextmanager
def request_context(
    *, request_id: str | None = None, tenant_id: str | None = None
) -> Iterator[None]:
    """Add a request ID and verified tenant ID to logs written inside this block."""
    request_token = _request_id.set(request_id)
    tenant_token = _tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _request_id.reset(request_token)
        _tenant_id.reset(tenant_token)


@contextmanager
def tenant_context(tenant_id: str) -> Iterator[None]:
    """Add a verified tenant ID to logs inside this block, keeping the current request ID."""
    token = _tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id.reset(token)


class JsonFormatter(logging.Formatter):
    """Format each record as one line of redacted JSON."""

    def __init__(self, service: ServiceInfo) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: value for key, value in vars(record).items() if key not in _STANDARD_ATTRIBUTES
        }
        entry: dict[str, Any] = dict(redact(extras))
        entry.update(
            time=datetime.fromtimestamp(record.created, UTC).isoformat(),
            severity=record.levelname,
            service=self._service.name,
            environment=self._service.environment,
            version=self._service.version,
            logger=record.name,
            message=redact_text(record.getMessage()),
        )
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            entry["trace_id"] = format(span_context.trace_id, "032x")
            entry["span_id"] = format(span_context.span_id, "016x")
        request_id = _request_id.get()
        if request_id is not None:
            entry["request_id"] = request_id
        tenant_id = _tenant_id.get()
        if tenant_id is not None:
            entry["tenant_id"] = tenant_id
        if record.exc_info:
            entry["exception"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            entry["stack"] = redact_text(self.formatStack(record.stack_info))
        return json.dumps(entry, default=lambda value: redact_text(str(value)))


def configure_logging(
    service: ServiceInfo, *, level: int = logging.INFO, stream: TextIO | None = None
) -> None:
    """Send all logs to one stream as JSON, where Fluent Bit collects them."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
