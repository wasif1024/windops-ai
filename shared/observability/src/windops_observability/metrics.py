"""Metrics shared by every service."""

from collections.abc import Callable

from opentelemetry.metrics import MeterProvider

_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def breaker_state_metrics(meter_provider: MeterProvider) -> Callable[[str, str, str], None]:
    """Return a CircuitBreaker `on_state_change` hook that exports metrics (RES-001).

    `windops.circuit_breaker.state` is 0 when closed, 1 when half-open and 2 when open.
    """
    meter = meter_provider.get_meter("windops.resilience")
    state_gauge = meter.create_gauge(
        "windops.circuit_breaker.state",
        description="Circuit breaker state: 0 closed, 1 half-open, 2 open",
    )
    transitions = meter.create_counter(
        "windops.circuit_breaker.transitions",
        description="Circuit breaker state changes",
    )

    def record(dependency: str, previous: str, current: str) -> None:
        state_gauge.set(_STATE_VALUES[current], {"dependency": dependency})
        transitions.add(1, {"dependency": dependency, "from_state": previous, "to_state": current})

    return record
