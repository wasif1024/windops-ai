"""What every console module shares: the call type, ID patterns, the idempotency key and times."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Header, Path
from google.protobuf.timestamp_pb2 import Timestamp

# What Keycloak and the services use as IDs and role names. The services check again.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
# A tenant ID, as the Tenant Service accepts it.
TENANT_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"

# (service, method, request, caller, retry) -> the service's answer
Call = Callable[..., Awaitable[Any]]

MemberId = Annotated[str, Path(pattern=ID_PATTERN)]
TenantId = Annotated[str, Path(pattern=TENANT_PATTERN)]

# Every change carries one, so the browser can safely send it again (GRP-007).
Key = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        pattern=ID_PATTERN,
        description="A new random value per change, the same when resending it",
    ),
]


def time_of(message: Any, name: str) -> str | None:
    """A timestamp field as ISO 8601 in UTC, or None when it is unset."""
    if not message.HasField(name):
        return None
    stamp: Timestamp = getattr(message, name)
    return stamp.ToDatetime().isoformat() + "Z"
