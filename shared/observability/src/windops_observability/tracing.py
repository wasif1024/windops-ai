"""Configure OpenTelemetry traces and metrics, and redact spans before export."""

from collections.abc import Sequence
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from windops_observability.redaction import redact, redact_text
from windops_observability.service import ServiceInfo

# A collector outage must not hold up a service's shutdown or its work.
_EXPORT_TIMEOUT_SECONDS = 2


def _redact_span(span: ReadableSpan) -> ReadableSpan:
    events = [
        Event(
            name=event.name,
            attributes=redact(dict(event.attributes or {})),
            timestamp=event.timestamp,
        )
        for event in span.events
    ]
    status = span.status
    if status.description:
        status = trace.Status(status.status_code, redact_text(status.description))
    return ReadableSpan(
        name=redact_text(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redact(dict(span.attributes or {})),
        events=events,
        links=span.links,
        kind=span.kind,
        instrumentation_scope=span.instrumentation_scope,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
    )


class RedactingSpanExporter(SpanExporter):
    """Remove sensitive values from spans before another exporter sends them (OBS-003)."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._exporter.export([_redact_span(span) for span in spans])

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)


@dataclass(frozen=True)
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider

    def shutdown(self) -> None:
        """Send anything still buffered. Call this during graceful shutdown."""
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def configure_telemetry(
    service: ServiceInfo,
    *,
    otlp_endpoint: str | None = None,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    install_global: bool = True,
) -> Telemetry:
    """Create trace and metric providers for a service.

    Without an endpoint or test exporter, spans and metrics are created but not sent.
    Every exported span is redacted.
    """
    resource = Resource.create(
        {
            "service.name": service.name,
            "service.version": service.version,
            "deployment.environment": service.environment,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        tracer_provider.add_span_processor(
            SimpleSpanProcessor(RedactingSpanExporter(span_exporter))
        )
    if otlp_endpoint is not None:
        endpoint = otlp_endpoint.rstrip("/")
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                RedactingSpanExporter(
                    OTLPSpanExporter(
                        endpoint=f"{endpoint}/v1/traces", timeout=_EXPORT_TIMEOUT_SECONDS
                    )
                )
            )
        )

    readers: list[MetricReader] = []
    if metric_reader is not None:
        readers.append(metric_reader)
    if otlp_endpoint is not None:
        exporter = OTLPMetricExporter(
            endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics", timeout=_EXPORT_TIMEOUT_SECONDS
        )
        readers.append(PeriodicExportingMetricReader(exporter))
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)

    if install_global:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)
