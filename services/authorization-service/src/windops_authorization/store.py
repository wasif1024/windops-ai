"""The tables this service owns, and the queries a decision needs. A migration creates them."""

import hashlib
import uuid
from datetime import UTC, datetime

from google.protobuf.message import Message
from sqlalchemy import (
    Column,
    DateTime,
    LargeBinary,
    MetaData,
    Table,
    Text,
    and_,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

from windops_authorization.policy import (
    DEFAULT_MATRIX,
    PLATFORM_ADMINISTRATOR,
    ROLES,
    Grant,
    Scope,
)

SERVICE_NAME = "authorization"
METADATA = MetaData()
# Grants are ended, never deleted, so Approval can check the roles held at a given time (RBAC-005).
ROLE_GRANTS = Table(
    "role_grants",
    METADATA,
    Column("grant_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("wind_farm_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("granted_by", Text, nullable=False),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("revoked_by", Text),
    Column("revoked_at", DateTime(timezone=True)),
)
# A tenant's own mapping. A tenant with no row in CUSTOMIZED_TENANTS uses the SRS 3.2 defaults.
ROLE_PERMISSIONS = Table(
    "role_permissions",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("role", Text, primary_key=True),
    Column("permission", Text, primary_key=True),
)
CUSTOMIZED_TENANTS = Table(
    "customized_tenants",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("customized_by", Text, nullable=False),
    Column("customized_at", DateTime(timezone=True), nullable=False),
)
BREAK_GLASS_GRANTS = Table(
    "break_glass_grants",
    METADATA,
    Column("grant_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ended_by", Text),
    Column("ended_at", DateTime(timezone=True)),
)

IDEMPOTENCY_KEYS = Table(
    "idempotency_keys",
    METADATA,
    Column("user_id", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("method", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("response", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def request_hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def remembered(
    connection: AsyncConnection, user_id: str, key: str, method: str, request: Message
) -> bytes | None:
    """Return the first answer to a retried call, or None if this key is new."""
    if not key:
        return None
    row = (
        await connection.execute(
            select(IDEMPOTENCY_KEYS).where(
                IDEMPOTENCY_KEYS.c.user_id == user_id,
                IDEMPOTENCY_KEYS.c.idempotency_key == key,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    if row.method != method or row.request_hash != request_hash(request):
        raise KeyReusedError("This idempotency key was used for a different request")
    response: bytes = row.response
    return response


async def remember(
    connection: AsyncConnection,
    user_id: str,
    key: str,
    method: str,
    request: Message,
    response: Message,
    now: datetime,
) -> None:
    """Keep the answer in the same transaction as the change, so a retry finds one or neither."""
    if key:
        await connection.execute(
            insert(IDEMPOTENCY_KEYS).values(
                user_id=user_id,
                idempotency_key=key,
                method=method,
                request_hash=request_hash(request),
                response=response.SerializeToString(),
                created_at=now,
            )
        )


def aware(moment: datetime) -> datetime:
    """Return a time with its zone, which some drivers drop."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def new_id() -> str:
    return str(uuid.uuid4())


async def active_grants(connection: AsyncConnection, tenant_id: str, user_id: str) -> list[Grant]:
    rows = await connection.execute(
        select(ROLE_GRANTS.c.role, ROLE_GRANTS.c.wind_farm_id, ROLE_GRANTS.c.turbine_id).where(
            ROLE_GRANTS.c.tenant_id == tenant_id,
            ROLE_GRANTS.c.user_id == user_id,
            ROLE_GRANTS.c.revoked_at.is_(None),
        )
    )
    return [Grant(row.role, Scope(row.wind_farm_id, row.turbine_id)) for row in rows]


async def custom_mapping(
    connection: AsyncConnection, tenant_id: str
) -> dict[str, frozenset[str]] | None:
    """Return a tenant's own mapping, or None while it uses the defaults."""
    customized = await connection.scalar(
        select(CUSTOMIZED_TENANTS.c.tenant_id).where(CUSTOMIZED_TENANTS.c.tenant_id == tenant_id)
    )
    if customized is None:
        return None
    rows = await connection.execute(
        select(ROLE_PERMISSIONS.c.role, ROLE_PERMISSIONS.c.permission).where(
            ROLE_PERMISSIONS.c.tenant_id == tenant_id
        )
    )
    mapping: dict[str, set[str]] = {}
    for row in rows:
        mapping.setdefault(row.role, set()).add(row.permission)
    return {role: frozenset(permissions) for role, permissions in mapping.items()}


async def replace_role_permissions(
    connection: AsyncConnection,
    tenant_id: str,
    role: str,
    permissions: frozenset[str],
    *,
    actor: str,
    now: datetime,
) -> None:
    """Change one role. The first change copies the defaults, so the other roles stay the same."""
    if await custom_mapping(connection, tenant_id) is None:
        await connection.execute(
            insert(CUSTOMIZED_TENANTS).values(
                tenant_id=tenant_id, customized_by=actor, customized_at=now
            )
        )
        rows = [
            {"tenant_id": tenant_id, "role": each, "permission": permission}
            for each in ROLES
            if each != PLATFORM_ADMINISTRATOR
            for permission in DEFAULT_MATRIX[each]
        ]
        await connection.execute(insert(ROLE_PERMISSIONS), rows)
    await connection.execute(
        delete(ROLE_PERMISSIONS).where(
            ROLE_PERMISSIONS.c.tenant_id == tenant_id, ROLE_PERMISSIONS.c.role == role
        )
    )
    if permissions:
        await connection.execute(
            insert(ROLE_PERMISSIONS),
            [{"tenant_id": tenant_id, "role": role, "permission": p} for p in permissions],
        )


async def break_glass_until(
    connection: AsyncConnection, tenant_id: str, user_id: str, now: datetime
) -> datetime | None:
    expires = await connection.scalar(
        select(BREAK_GLASS_GRANTS.c.expires_at)
        .where(
            BREAK_GLASS_GRANTS.c.tenant_id == tenant_id,
            BREAK_GLASS_GRANTS.c.user_id == user_id,
            BREAK_GLASS_GRANTS.c.ended_at.is_(None),
            BREAK_GLASS_GRANTS.c.expires_at > now,
        )
        .order_by(BREAK_GLASS_GRANTS.c.expires_at.desc())
        .limit(1)
    )
    return aware(expires) if expires is not None else None


async def find_active_grant(
    connection: AsyncConnection, tenant_id: str, user_id: str, role: str, scope: Scope
) -> str | None:
    found: str | None = await connection.scalar(
        select(ROLE_GRANTS.c.grant_id).where(
            ROLE_GRANTS.c.tenant_id == tenant_id,
            ROLE_GRANTS.c.user_id == user_id,
            ROLE_GRANTS.c.role == role,
            ROLE_GRANTS.c.wind_farm_id == scope.wind_farm_id,
            ROLE_GRANTS.c.turbine_id == scope.turbine_id,
            ROLE_GRANTS.c.revoked_at.is_(None),
        )
    )
    return found


async def add_grant(
    connection: AsyncConnection,
    tenant_id: str,
    user_id: str,
    role: str,
    scope: Scope,
    *,
    actor: str,
    now: datetime,
) -> str:
    grant_id = new_id()
    await connection.execute(
        insert(ROLE_GRANTS).values(
            grant_id=grant_id,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            wind_farm_id=scope.wind_farm_id,
            turbine_id=scope.turbine_id,
            granted_by=actor,
            granted_at=now,
        )
    )
    return grant_id


async def other_whole_tenant_holders(
    connection: AsyncConnection, tenant_id: str, role: str, grant_id: str
) -> int:
    """Count the other active, tenant-wide grants of a role, so the last one is not removed."""
    count = await connection.scalar(
        select(ROLE_GRANTS.c.grant_id)
        .where(
            and_(
                ROLE_GRANTS.c.tenant_id == tenant_id,
                ROLE_GRANTS.c.role == role,
                ROLE_GRANTS.c.wind_farm_id == "",
                ROLE_GRANTS.c.turbine_id == "",
                ROLE_GRANTS.c.revoked_at.is_(None),
                ROLE_GRANTS.c.grant_id != grant_id,
            )
        )
        .limit(1)
    )
    return 0 if count is None else 1


async def end_grant(
    connection: AsyncConnection, grant_id: str, *, actor: str, now: datetime
) -> None:
    await connection.execute(
        update(ROLE_GRANTS)
        .where(ROLE_GRANTS.c.grant_id == grant_id)
        .values(revoked_by=actor, revoked_at=now)
    )


async def audit(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    action: str,
    actor_id: str,
    resource_type: str,
    resource_id: str,
    outcome: str,
    actor_type: str = "user",
) -> None:
    """Queue an audit record in the same transaction as the change it describes (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )
