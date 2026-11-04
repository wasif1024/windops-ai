# windops-resilience

Circuit breakers, retries and deadlines for remote calls, using the SRS 7.7 defaults.

```python
from windops_resilience import BreakerConfig, CircuitBreaker, call_with_resilience

# One breaker per dependency in each service. The SRS sets no slow-call limit, so choose one.
telemetry_breaker = CircuitBreaker("telemetry", BreakerConfig(slow_call_seconds=2.0))


async def create_snapshot(request):
    return await call_with_resilience(
        telemetry_breaker,
        lambda remaining: stub.CreateTelemetrySnapshot(request, timeout=remaining),
        idempotent=True,  # The request carries an idempotency key.
        deadline_seconds=5.0,
    )
```

## Behavior

| Setting | Default |
| --- | --- |
| Failure window | Rolling 20 calls, minimum 10 |
| Open threshold | 50% failures or slow calls |
| Open duration | 30 seconds, then half-open |
| Half-open probes | 3 calls, and all must succeed to close |
| Retries | At most 3 total attempts, idempotent calls only, exponential backoff with jitter |

- **Counts as a failure:** `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED`, `INTERNAL`,
  HTTP 429 and 5xx, timeouts, connection errors and slow calls.
- **Never counts:** `INVALID_ARGUMENT`, `NOT_FOUND`, `PERMISSION_DENIED`, `UNAUTHENTICATED`, other
  HTTP errors, and other exceptions.
- **Open breaker:** raises `BreakerOpenError` without calling the dependency. Each service
  defines its SRS 7.7 fallback by handling that error.

## Rules for callers

- Pass the remaining time to the client call, so its deadline matches the overall one.
- Disable a client SDK's own retries, so retry layers do not multiply (RES-003).
- Do not treat Kubernetes probes as a substitute for a breaker (RES-006).
- `on_state_change` reports each state change. The observability library exports it as a metric.
- Use a breaker on one event loop. It is not safe to share across threads.
