"""A service acting as itself, with no user behind it (ADR 0008)."""

import contextlib
from collections.abc import Iterator

from windops_auth import identity_scope, sign_identity
from windops_auth.identity import VerifiedIdentity
from windops_observability import tenant_context

SERVICE_PREFIX = "service:"


def service_subject(service: str) -> str:
    return f"{SERVICE_PREFIX}{service}"


@contextlib.contextmanager
def acting_as_service(service: str, tenant: str, secret: str) -> Iterator[None]:
    """Calls made inside go out as the service itself, in this tenant. The Authorization Service
    allows it only that service's fixed read grants."""
    identity = VerifiedIdentity(subject=service_subject(service), tenant_id=tenant)
    token = sign_identity(identity, secret, ttl_seconds=120)
    with identity_scope(identity, token), tenant_context(tenant):
        yield
