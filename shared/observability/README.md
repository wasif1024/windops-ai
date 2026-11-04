# windops-observability

Structured logs, traces and metrics for WindOps services.

```python
from windops_observability import (
    ServiceInfo,
    breaker_state_metrics,
    configure_logging,
    configure_telemetry,
    request_context,
)

service = ServiceInfo(name="telemetry", environment="local", version="1.0.0")
configure_logging(service)
telemetry = configure_telemetry(service, otlp_endpoint="http://windops-otel-collector:4318")

# Pass this to a CircuitBreaker to export its state.
on_state_change = breaker_state_metrics(telemetry.meter_provider)

with request_context(request_id=request_id, tenant_id=verified_tenant_id):
    logger.info("Created snapshot", extra={"turbine_id": turbine_id})

telemetry.shutdown()  # During graceful shutdown.
```

## Logs

Each record is one line of JSON with `time`, `severity`, `service`, `environment`, `version`,
`logger` and `message`. It adds `trace_id`, `span_id`, `request_id`, `tenant_id`, `exception`
and `extra` fields when they apply. `extra` fields cannot replace the required fields.

## Redaction

Logs and exported spans remove:

- **Sensitive keys:** such as `password`, `access_token`, `api_key`, `authorization`, `cookie`
  and `email`. Counters such as `input_tokens` are kept.
- **Sensitive text:** bearer tokens, JWTs, common provider keys, private keys, URL passwords,
  email addresses, and `password=...` style pairs.

Redaction is a safety net that recognizes known patterns. It cannot catch everything, so do not
pass secrets or unnecessary personal data to a log or span. Metric attributes are not redacted, so
use only low-risk values such as a dependency name.

## Metrics

| Metric | Meaning |
| --- | --- |
| `windops.circuit_breaker.state` | 0 closed, 1 half-open, 2 open, per `dependency` |
| `windops.circuit_breaker.transitions` | State changes, per `dependency`, `from_state` and `to_state` |
