import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from windops_observability import ServiceInfo, breaker_state_metrics, configure_telemetry
from windops_resilience import BreakerConfig, CircuitBreaker

SERVICE = ServiceInfo(name="telemetry", environment="test", version="1.2.3")


def test_spans_carry_service_identity() -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(SERVICE, span_exporter=exporter, install_global=False)
    with telemetry.tracer_provider.get_tracer("test").start_as_current_span("work"):
        pass
    resource = exporter.get_finished_spans()[0].resource.attributes
    assert resource["service.name"] == "telemetry"
    assert resource["service.version"] == "1.2.3"
    assert resource["deployment.environment"] == "test"


def test_exported_spans_are_redacted() -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(SERVICE, span_exporter=exporter, install_global=False)
    with telemetry.tracer_provider.get_tracer("test").start_as_current_span("work") as span:
        span.set_attribute("api_key", "abc123")
        span.set_attribute("note", "Bearer abc123")
        span.set_attribute("input_tokens", 42)
        span.add_event("called", {"authorization": "abc123"})
        span.set_status(trace.Status(trace.StatusCode.ERROR, "password=abc123"))
    exported = exporter.get_finished_spans()[0]
    assert exported.attributes is not None
    assert exported.attributes["api_key"] == "[REDACTED]"
    assert exported.attributes["note"] == "[REDACTED]"
    assert exported.attributes["input_tokens"] == 42
    assert exported.events[0].attributes is not None
    assert exported.events[0].attributes["authorization"] == "[REDACTED]"
    assert "abc123" not in (exported.status.description or "")


def test_recorded_exceptions_are_redacted() -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(SERVICE, span_exporter=exporter, install_global=False)
    tracer = telemetry.tracer_provider.get_tracer("test")
    with pytest.raises(ValueError), tracer.start_as_current_span("work"):
        raise ValueError("Failed with token=abc123")
    exported = exporter.get_finished_spans()[0]
    assert "abc123" not in str(exported.to_json())


def test_trace_context_propagates_across_a_call() -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(SERVICE, span_exporter=exporter, install_global=False)
    tracer = telemetry.tracer_provider.get_tracer("test")
    carrier: dict[str, str] = {}
    with tracer.start_as_current_span("client"):
        inject(carrier)
    assert "traceparent" in carrier
    with tracer.start_as_current_span("server", context=extract(carrier)):
        pass
    server, client = exporter.get_finished_spans()[1], exporter.get_finished_spans()[0]
    assert server.context is not None
    assert client.context is not None
    assert server.context.trace_id == client.context.trace_id


def metric_points(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, str], float]]]:
    found: dict[str, list[tuple[dict[str, str], float]]] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found[metric.name] = [
                    (dict(point.attributes or {}), point.value)  # type: ignore[union-attr, arg-type]
                    for point in metric.data.data_points
                ]
    return found


@pytest.mark.parametrize(("current", "value"), [("closed", 0), ("half_open", 1), ("open", 2)])
def test_breaker_hook_records_each_state(current: str, value: int) -> None:
    reader = InMemoryMetricReader()
    telemetry = configure_telemetry(SERVICE, metric_reader=reader, install_global=False)
    breaker_state_metrics(telemetry.meter_provider)("weather", "closed", current)
    points = metric_points(reader)
    assert points["windops.circuit_breaker.state"] == [({"dependency": "weather"}, value)]
    assert points["windops.circuit_breaker.transitions"] == [
        ({"dependency": "weather", "from_state": "closed", "to_state": current}, 1)
    ]


def test_real_breaker_exports_its_state() -> None:
    reader = InMemoryMetricReader()
    telemetry = configure_telemetry(SERVICE, metric_reader=reader, install_global=False)
    breaker = CircuitBreaker(
        "weather",
        BreakerConfig(slow_call_seconds=5.0, minimum_calls=1),
        on_state_change=breaker_state_metrics(telemetry.meter_provider),
    )

    async def fail() -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        asyncio.run(breaker.call(fail))
    assert metric_points(reader)["windops.circuit_breaker.state"] == [
        ({"dependency": "weather"}, 2)
    ]


class _Collector(BaseHTTPRequestHandler):
    received: list[tuple[str, bytes]]

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.received.append((self.path, self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def collector() -> Iterator[tuple[str, list[tuple[str, bytes]]]]:
    received: list[tuple[str, bytes]] = []
    handler = type("Handler", (_Collector,), {"received": received})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()
    thread.join()
    server.server_close()


def test_otlp_export_reaches_a_collector_with_secrets_removed(
    collector: tuple[str, list[tuple[str, bytes]]],
) -> None:
    endpoint, received = collector
    telemetry = configure_telemetry(SERVICE, otlp_endpoint=endpoint, install_global=False)
    with telemetry.tracer_provider.get_tracer("test").start_as_current_span("work") as span:
        span.set_attribute("api_key", "supersecretvalue")
    breaker_state_metrics(telemetry.meter_provider)("weather", "closed", "open")
    telemetry.shutdown()
    paths = {path for path, _ in received}
    assert paths == {"/v1/traces", "/v1/metrics"}
    traces = b"".join(body for path, body in received if path == "/v1/traces")
    assert b"telemetry" in traces
    assert b"[REDACTED]" in traces
    assert b"supersecretvalue" not in traces
    assert b"windops.circuit_breaker.state" in b"".join(body for _, body in received)


def test_no_exporter_is_safe() -> None:
    telemetry = configure_telemetry(SERVICE, install_global=False)
    with telemetry.tracer_provider.get_tracer("test").start_as_current_span("work"):
        pass
    telemetry.shutdown()
