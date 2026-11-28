"""Circuit breakers, bulkheads, retries and deadlines for remote calls (SRS section 7.7)."""

from windops_resilience.breaker import BreakerConfig, BreakerState, CircuitBreaker
from windops_resilience.bulkhead import Bulkhead, BulkheadConfig, BulkheadFullError
from windops_resilience.classify import is_dependency_failure
from windops_resilience.errors import BreakerOpenError
from windops_resilience.retry import RetryPolicy, call_with_resilience

__all__ = [
    "BreakerConfig",
    "BreakerOpenError",
    "BreakerState",
    "Bulkhead",
    "BulkheadConfig",
    "BulkheadFullError",
    "CircuitBreaker",
    "RetryPolicy",
    "call_with_resilience",
    "is_dependency_failure",
]
