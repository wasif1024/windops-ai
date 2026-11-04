"""Tenant-scoped Redis cache and rate limits (SRS 8.1, DAT-004, DAT-007)."""

from windops_cache.cache import (
    CacheUnavailableError,
    RateLimitDecision,
    RateLimiter,
    RateLimitUnavailableError,
    TenantCache,
    cache_breaker,
    connect,
)
from windops_cache.keys import MAX_TTL_SECONDS, build_key

__all__ = [
    "MAX_TTL_SECONDS",
    "CacheUnavailableError",
    "RateLimitDecision",
    "RateLimitUnavailableError",
    "RateLimiter",
    "TenantCache",
    "build_key",
    "cache_breaker",
    "connect",
]
