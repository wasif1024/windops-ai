"""Device credentials: one per turbine, scoped to its tenant (FR-TEL-005).

A device sends `Authorization: Bearer <tenant>.<device>.<secret>`. The tenant comes first, so the
credential is looked up inside that tenant, with row-level security on. Only the secret's SHA-256
is stored: the secret is 256 random bits, so a slow hash would add nothing.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

TOKEN = re.compile(r"^([a-z0-9][a-z0-9-]{1,62})\.(dev_[0-9a-f]{16})\.([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True)
class Presented:
    tenant_id: str
    device_id: str
    secret: str


def new_device_id() -> str:
    return f"dev_{secrets.token_hex(8)}"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def token(tenant_id: str, device_id: str, secret: str) -> str:
    return f"{tenant_id}.{device_id}.{secret}"


def hashed(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def matches(secret: str, stored_hash: str) -> bool:
    """In constant time, so a guess learns nothing from how long the answer takes."""
    return hmac.compare_digest(hashed(secret), stored_hash)


def presented(header: str | None) -> Presented | None:
    """The credential in an Authorization header, or None if there is none in the right shape."""
    if not header or not header.startswith("Bearer "):
        return None
    found = TOKEN.match(header.removeprefix("Bearer ").strip())
    if found is None:
        return None
    return Presented(found.group(1), found.group(2), found.group(3))
