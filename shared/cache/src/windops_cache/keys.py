"""Build cache keys that always carry their service and tenant (DAT-007)."""

import re

MAX_TTL_SECONDS = 24 * 60 * 60

_SERVICE = re.compile(r"[a-z]+(-[a-z]+)*")
_TENANT = re.compile(r"[A-Za-z0-9_-]+")
_NAME = re.compile(r"[A-Za-z0-9_.:-]+")
# DAT-004: credentials stay out of the cache. A name check cannot see values, so it catches the
# honest mistake of naming what is being stored.
_SENSITIVE = re.compile(r"token|passw|secret|credential|api[_-]?key|session", re.IGNORECASE)


def build_key(service: str, tenant_id: str, name: str) -> str:
    """Return `<service>:<tenant>:<name>`, refusing anything that could escape that shape."""
    if not _SERVICE.fullmatch(service):
        raise ValueError(f"Unsupported service name: {service!r}")
    # A tenant cannot contain ":", so one tenant's key can never look like another's.
    if not _TENANT.fullmatch(tenant_id):
        raise ValueError("Unsupported tenant identifier")
    if not _NAME.fullmatch(name):
        raise ValueError(f"Unsupported cache key name: {name!r}")
    if _SENSITIVE.search(name):
        raise ValueError(f"Credentials must not be cached (DAT-004): {name!r}")
    return f"{service}:{tenant_id}:{name}"


def validate_ttl(ttl_seconds: int) -> int:
    """Return a TTL, refusing one that would let an entry outlive its purpose."""
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"A TTL must be between 1 and {MAX_TTL_SECONDS} seconds")
    return ttl_seconds
