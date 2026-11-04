"""Circuit breakers, retries and deadlines for remote calls (SRS section 7.7)."""

from windops_resilience.breaker import (
    BreakerConfig,
    BreakerOpenError,
    BreakerState,
    CircuitBreaker,
)
from windops_resilience.classify import is_dependency_failure
from windops_resilience.retry import RetryPolicy, call_with_resilience

__all__ = [
    "BreakerConfig",
    "BreakerOpenError",
    "BreakerState",
    "CircuitBreaker",
    "RetryPolicy",
    "call_with_resilience",
    "is_dependency_failure",
]
