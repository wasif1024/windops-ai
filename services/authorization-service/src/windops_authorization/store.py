"""The tables this service owns, and the queries a decision needs. A migration creates them."""

import hashlib
import uuid
from datetime import UTC, datetime

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    LargeBinary,
    MetaData,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_migrations import in_tenant
from windops_outbox import add_event

from windops_authorization.policy import (
    DEFAULT_MATRIX,
    PLATFORM_ADMINISTRATOR,
    ROLES,
    TENANT_ADMINISTRATOR,
    Grant,
    Scope,
    permissions_of,
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
# Every change to a role's permissions, as it happened, so a decision names the version it was
# made on (RBAC-005), and a change cannot make its own author eligible for an earlier request
# (RBAC-006). Each keeps what it replaced, comma-separated and sorted.
ROLE_PERMISSION_CHANGES = Table(
    "role_permission_changes",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("version", BigInteger, primary_key=True),
    Column("role", Text, nullable=False),
    Column("previous", Text, nullable=False),
    Column("permissions", Text, nullable=False),
    Column("changed_by", Text, nullable=False),
    Column("changed_at", DateTime(timezone=True), nullable=False),
)
# Changes to who may approve maintenance, waiting for a second person (RBAC-007). The request is
# the serialized GrantRoleRequest, RevokeRoleRequest or SetRolePermissionsRequest it applies.
PENDING_CHANGES = Table(
    "pending_changes",
    METADATA,
    Column("change_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("request", LargeBinary, nullable=False),
    Column("role", Text, nullable=False),
    Column("grantee_id", Text, nullable=False, server_default=""),
    Column("grant_id", Text, nullable=False, server_default=""),
    Column("permissions", Text, nullable=False, server_default=""),
    Column("proposed_by", Text, nullable=False),
    Column("proposed_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("escalated", Boolean, nullable=False),
    Column("decided_by", Text, nullable=False, server_default=""),
    Column("decided_at", DateTime(timezone=True)),
    Column("decline_reason", Text, nullable=False, server_default=""),
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


async def active_grants(
    connection: AsyncConnection,
    tenant_id: str,
    user_id: str,
    *,
    not_self_after: datetime | None = None,
) -> list[Grant]:
    """With not_self_after, leaving out the roles the user granted themselves after then
    (RBAC-006)."""
    await in_tenant(connection, tenant_id)
    g = ROLE_GRANTS.c
    query = select(g.role, g.wind_farm_id, g.turbine_id).where(
        g.tenant_id == tenant_id, g.user_id == user_id, g.revoked_at.is_(None)
    )
    if not_self_after is not None:
        query = query.where(~and_(g.granted_by == user_id, g.granted_at > not_self_after))
    rows = await connection.execute(query)
    return [Grant(row.role, Scope(row.wind_farm_id, row.turbine_id)) for row in rows]


async def permissions_version(connection: AsyncConnection, tenant_id: str) -> int:
    """0 for the SRS 3.2 defaults, then one more for each change (RBAC-005)."""
    await in_tenant(connection, tenant_id)
    c = ROLE_PERMISSION_CHANGES.c
    found = await connection.scalar(select(func.max(c.version)).where(c.tenant_id == tenant_id))
    return int(found or 0)


async def mapping_without_own_changes(
    connection: AsyncConnection, tenant_id: str, author: str, since: datetime,
    custom: dict[str, frozenset[str]] | None,
) -> dict[str, frozenset[str]] | None:  # fmt: skip
    """The tenant's mapping with the author's own changes since then undone: each role they
    changed is as it was before their first change after then (RBAC-006)."""
    if custom is None:
        return None
    c = ROLE_PERMISSION_CHANGES.c
    rows = (await connection.execute(
        select(c.role, c.previous).where(c.tenant_id == tenant_id, c.changed_by == author,
                                         c.changed_at > since).order_by(c.version)
    )).all()  # fmt: skip
    undone, seen = dict(custom), set()
    for row in rows:
        if row.role not in seen:
            seen.add(row.role)
            undone[row.role] = frozenset(p for p in row.previous.split(",") if p)
    return undone


async def custom_mapping(
    connection: AsyncConnection, tenant_id: str
) -> dict[str, frozenset[str]] | None:
    """Return a tenant's own mapping, or None while it uses the defaults."""
    await in_tenant(connection, tenant_id)
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
    """Change one role, keeping the change. The first change copies the defaults, so the other
    roles stay the same."""
    await in_tenant(connection, tenant_id)
    before = await custom_mapping(connection, tenant_id)
    version = await permissions_version(connection, tenant_id) + 1
    await connection.execute(insert(ROLE_PERMISSION_CHANGES).values(
        tenant_id=tenant_id, version=version, role=role,
        previous=",".join(sorted(permissions_of(role, before))),
        permissions=",".join(sorted(permissions)), changed_by=actor, changed_at=now,
    ))  # fmt: skip
    if before is None:
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
    await in_tenant(connection, tenant_id)
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
    await in_tenant(connection, tenant_id)
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
    await in_tenant(connection, tenant_id)
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


async def tenant_administrators(
    connection: AsyncConnection, tenant_id: str, *, besides: str = ""
) -> int:
    """How many people hold the Tenant Administrator role for the whole tenant, besides one."""
    await in_tenant(connection, tenant_id)
    g = ROLE_GRANTS.c
    found = await connection.scalar(
        select(func.count(func.distinct(g.user_id))).where(
            g.tenant_id == tenant_id, g.role == TENANT_ADMINISTRATOR, g.wind_farm_id == "",
            g.turbine_id == "", g.revoked_at.is_(None), g.user_id != besides,
        )
    )  # fmt: skip
    return int(found or 0)


async def other_whole_tenant_holders(
    connection: AsyncConnection, tenant_id: str, role: str, grant_id: str
) -> int:
    """Count the other active, tenant-wide grants of a role, so the last one is not removed."""
    await in_tenant(connection, tenant_id)
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
    on_behalf_of: str = "",
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
            on_behalf_of=on_behalf_of,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )
