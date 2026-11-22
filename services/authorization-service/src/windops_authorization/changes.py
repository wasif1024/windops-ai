"""Changes to roles and their permissions: applied at once, or held for a second person.

A change to who may approve maintenance is approval policy (RBAC-007, ADR 0035): granting or
revoking a role that holds maintenance.approve in the tenant, or giving that permission to a role
or taking it away. It waits for a second Tenant Administrator, or, where the tenant has no other,
a Platform Administrator. Applied, it stays the proposer's change, so it can never make them
eligible for an approval requested before it (RBAC-006).
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.authorization.v1 import authorization_pb2 as pb
from windops_grpc import ConflictError, NotFoundError
from windops_migrations import in_tenant

from windops_authorization import policy
from windops_authorization.policy import Scope
from windops_authorization.store import (
    PENDING_CHANGES,
    ROLE_GRANTS,
    add_grant,
    audit,
    aware,
    end_grant,
    find_active_grant,
    new_id,
    other_whole_tenant_holders,
    replace_role_permissions,
    tenant_administrators,
)

APPROVE = "maintenance.approve"
GRANT, REVOKE, ROLE_PERMISSIONS = "grant", "revoke", "role_permissions"
PENDING, CONFIRMED, DECLINED, EXPIRED = "pending", "confirmed", "declined", "expired"
LIFETIME = timedelta(days=7)


def _stamp(moment: datetime | None) -> Timestamp | None:
    if moment is None:
        return None
    made = Timestamp()
    made.FromDatetime(aware(moment))
    return made


def grant_message(row: Any) -> pb.RoleGrant:
    return pb.RoleGrant(
        grant_id=row.grant_id, tenant_id=row.tenant_id, user_id=row.user_id, role=row.role,
        scope=pb.Scope(wind_farm_id=row.wind_farm_id, turbine_id=row.turbine_id),
        granted_by=row.granted_by, granted_at=_stamp(row.granted_at),
        revoked_by=row.revoked_by or "", revoked_at=_stamp(row.revoked_at),
    )  # fmt: skip


def status_of(row: Any, now: datetime) -> str:
    if row.status == PENDING and aware(row.expires_at) <= now:
        return EXPIRED
    return str(row.status)


def change_message(row: Any, now: datetime) -> pb.PendingChange:
    return pb.PendingChange(
        change_id=row.change_id, tenant_id=row.tenant_id, kind=row.kind, summary=row.summary,
        proposed_by=row.proposed_by, proposed_at=_stamp(row.proposed_at),
        expires_at=_stamp(row.expires_at), status=status_of(row, now),
        decided_by=row.decided_by, decided_at=_stamp(row.decided_at),
        decline_reason=row.decline_reason, escalated=row.escalated, role=row.role,
        grantee_id=row.grantee_id, grant_id=row.grant_id,
        permissions=[p for p in row.permissions.split(",") if p],
    )  # fmt: skip


def where(scope: Scope) -> str:
    if scope.turbine_id:
        return f"turbine {scope.turbine_id}"
    if scope.wind_farm_id:
        return f"wind farm {scope.wind_farm_id}"
    return "the whole tenant"


# --- Whether a change is approval policy (RBAC-007)


def role_approves(role: str, custom: Any) -> bool:
    return APPROVE in policy.permissions_of(role, custom)


def changes_approval(role: str, custom: Any, chosen: Iterable[str]) -> bool:
    return role_approves(role, custom) != (APPROVE in set(chosen))


# --- Holding one


async def propose(
    connection: AsyncConnection, tenant: str, kind: str, request: Any, *, summary: str,
    role: str, actor: str, now: datetime, grantee_id: str = "", grant_id: str = "",
    permissions: Iterable[str] = (),
) -> pb.PendingChange:  # fmt: skip
    """Hold the change for a second person. Escalated when the tenant has no other Tenant
    Administrator to confirm it."""
    escalated = await tenant_administrators(connection, tenant, besides=actor) == 0
    change_id = new_id()
    await connection.execute(PENDING_CHANGES.insert().values(
        change_id=change_id, tenant_id=tenant, kind=kind, summary=summary,
        request=request.SerializeToString(), role=role, grantee_id=grantee_id,
        grant_id=grant_id, permissions=",".join(sorted(permissions)), proposed_by=actor,
        proposed_at=now, expires_at=now + LIFETIME, status=PENDING, escalated=escalated,
    ))  # fmt: skip
    await audit(connection, tenant_id=tenant, action="authorization.approval_policy.proposed",
                actor_id=actor, resource_type="pending_change", resource_id=change_id,
                outcome="succeeded")  # fmt: skip
    return change_message(await load(connection, tenant, change_id), now)


async def load(connection: AsyncConnection, tenant: str, change_id: str) -> Any:
    await in_tenant(connection, tenant)
    c = PENDING_CHANGES.c
    row = (await connection.execute(
        select(PENDING_CHANGES).where(c.tenant_id == tenant, c.change_id == change_id)
    )).one_or_none()  # fmt: skip
    if row is None:
        raise NotFoundError("No such pending change in this tenant")
    return row


# --- Applying one, at once or once confirmed


async def apply_grant(connection: AsyncConnection, tenant: str, request: pb.GrantRoleRequest,
                      *, actor: str, now: datetime) -> pb.RoleGrant:  # fmt: skip
    scope = Scope(request.scope.wind_farm_id, request.scope.turbine_id)
    if await find_active_grant(connection, tenant, request.grantee_id, request.role, scope):
        raise ConflictError("The user already has this role here")
    grant_id = await add_grant(connection, tenant, request.grantee_id, request.role, scope,
                               actor=actor, now=now)  # fmt: skip
    await audit(connection, tenant_id=tenant, action=f"authorization.role.granted.{request.role}",
                actor_id=actor, resource_type="user", resource_id=request.grantee_id,
                outcome="succeeded")  # fmt: skip
    row = (
        await connection.execute(select(ROLE_GRANTS).where(ROLE_GRANTS.c.grant_id == grant_id))
    ).one()
    return grant_message(row)


async def active_grant(connection: AsyncConnection, tenant: str, grant_id: str) -> Any:
    await in_tenant(connection, tenant)
    row = (
        await connection.execute(
            select(ROLE_GRANTS).where(
                ROLE_GRANTS.c.grant_id == grant_id, ROLE_GRANTS.c.tenant_id == tenant
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("No such grant in this tenant")
    if row.revoked_at is not None:
        raise ConflictError("The grant has already ended")
    return row


async def apply_revoke(connection: AsyncConnection, tenant: str, grant_id: str, *, actor: str,
                       now: datetime) -> pb.RoleGrant:  # fmt: skip
    row = await active_grant(connection, tenant, grant_id)
    keeps_tenant_running = (
        row.role in (policy.TENANT_ADMINISTRATOR, policy.PLATFORM_ADMINISTRATOR)
        and Scope(row.wind_farm_id, row.turbine_id).whole_tenant
    )
    if keeps_tenant_running and not await other_whole_tenant_holders(
        connection, tenant, row.role, row.grant_id
    ):
        raise ConflictError("This would leave nobody able to manage roles")
    await end_grant(connection, row.grant_id, actor=actor, now=now)
    await audit(connection, tenant_id=tenant, action=f"authorization.role.revoked.{row.role}",
                actor_id=actor, resource_type="user", resource_id=row.user_id,
                outcome="succeeded")  # fmt: skip
    ended = (
        await connection.execute(select(ROLE_GRANTS).where(ROLE_GRANTS.c.grant_id == row.grant_id))
    ).one()
    return grant_message(ended)


async def apply_role_permissions(
    connection: AsyncConnection, tenant: str, role: str, chosen: frozenset[str], *, actor: str,
    now: datetime,
) -> pb.RolePermissions:  # fmt: skip
    await replace_role_permissions(connection, tenant, role, chosen, actor=actor, now=now)
    await audit(connection, tenant_id=tenant, action="authorization.role_permissions.changed",
                actor_id=actor, resource_type="role", resource_id=role,
                outcome="succeeded")  # fmt: skip
    return pb.RolePermissions(tenant_id=tenant, role=role, permissions=sorted(chosen),
                              customized=True)  # fmt: skip
