"""The gRPC API. Every rule lives in policy.py, and this checks who is asking before using it."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.authorization.v1 import authorization_pb2, authorization_pb2_grpc
from windops_auth import MfaRequiredError, authentication_claims, require_recent_mfa
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    VerifiedIdentity,
    current_identity,
)

from windops_authorization import policy
from windops_authorization.policy import Decision, RuleError, Scope
from windops_authorization.store import (
    BREAK_GLASS_GRANTS,
    ROLE_GRANTS,
    KeyReusedError,
    active_grants,
    add_grant,
    audit,
    aware,
    break_glass_until,
    custom_mapping,
    end_grant,
    find_active_grant,
    new_id,
    other_whole_tenant_holders,
    remember,
    remembered,
    replace_role_permissions,
)

_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_MAXIMUM_ID = 255
_MAXIMUM_KEY = 128
STEP_UP = "This operation needs a recent second factor"


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime | None) -> Timestamp | None:
    if moment is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(aware(moment))
    return stamp


def _scope(message: authorization_pb2.Scope) -> Scope:
    for value in (message.wind_farm_id, message.turbine_id):
        if len(value) > _MAXIMUM_ID:
            raise InvalidRequestError("A scope ID is too long")
    return Scope(message.wind_farm_id, message.turbine_id)


def _grant_message(row: Any) -> authorization_pb2.RoleGrant:
    return authorization_pb2.RoleGrant(
        grant_id=row.grant_id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        role=row.role,
        scope=authorization_pb2.Scope(wind_farm_id=row.wind_farm_id, turbine_id=row.turbine_id),
        granted_by=row.granted_by,
        granted_at=_timestamp(row.granted_at),
        revoked_by=row.revoked_by or "",
        revoked_at=_timestamp(row.revoked_at),
    )


def _break_glass_message(row: Any) -> authorization_pb2.BreakGlassGrant:
    return authorization_pb2.BreakGlassGrant(
        grant_id=row.grant_id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        reason=row.reason,
        started_at=_timestamp(row.started_at),
        expires_at=_timestamp(row.expires_at),
        ended_at=_timestamp(row.ended_at),
    )


async def _replayed(
    connection: AsyncConnection, user: str, method: str, request: Any, response_type: Any
) -> Any:
    """Return the first answer to a retry, or None for a new call (GRP-007)."""
    if len(request.idempotency_key) > _MAXIMUM_KEY:
        raise InvalidRequestError("The idempotency key is too long")
    try:
        stored = await remembered(connection, user, request.idempotency_key, method, request)
    except KeyReusedError as error:
        raise InvalidRequestError(str(error)) from error
    return None if stored is None else response_type.FromString(stored)


class AuthorizationService(authorization_pb2_grpc.AuthorizationServiceServicer):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _user(identity: VerifiedIdentity) -> str:
        if not identity.user_id:
            raise PermissionDeniedError("Only a user can do this")
        return identity.user_id

    @staticmethod
    def _target(identity: VerifiedIdentity, tenant_id: str) -> str:
        target = tenant_id or identity.tenant_id
        if not _TENANT.match(target):
            raise InvalidRequestError("The tenant ID is not valid")
        return target

    async def _decide(
        self,
        connection: AsyncConnection,
        identity: VerifiedIdentity,
        permission: str,
        tenant_id: str,
        scope: Scope,
    ) -> Decision:
        if not identity.user_id:
            return Decision(False, "No user in the identity")
        if tenant_id == identity.tenant_id:
            return policy.decide_in_tenant(
                permission,
                scope,
                await active_grants(connection, tenant_id, identity.user_id),
                await custom_mapping(connection, tenant_id),
            )
        platform = []
        if identity.tenant_id == policy.PLATFORM_TENANT:
            platform = await active_grants(connection, policy.PLATFORM_TENANT, identity.user_id)
        now = _now()
        return policy.decide_across_tenants(
            permission,
            platform_grants=platform,
            break_glass_until=await break_glass_until(connection, tenant_id, identity.user_id, now),
            now=now,
        )

    async def _refuse(self, identity: VerifiedIdentity, what: str, message: str) -> None:
        """Record a refusal in the caller's own tenant, then raise it (SRS 9.4)."""
        async with self._engine.begin() as connection:
            await audit(
                connection,
                tenant_id=identity.tenant_id,
                action="authorization.denied",
                actor_id=identity.user_id or identity.subject,
                resource_type="permission",
                resource_id=what,
                outcome="denied",
            )
        raise PermissionDeniedError(message)

    async def _require(
        self, permission: str, tenant_id: str, *, step_up: bool
    ) -> tuple[VerifiedIdentity, str, str]:
        """Return the caller, their user ID and the target tenant, or refuse."""
        identity = current_identity()
        target = self._target(identity, tenant_id)
        async with self._engine.connect() as connection:
            decision = await self._decide(connection, identity, permission, target, Scope())
        if not decision.allowed:
            await self._refuse(identity, permission, "You do not have permission to do this")
        if step_up:
            try:
                require_recent_mfa(authentication_claims(identity))
            except MfaRequiredError:
                await self._refuse(identity, permission, STEP_UP)
        return identity, self._user(identity), target

    async def Check(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.CheckRequest, context: Any
    ) -> authorization_pb2.CheckResponse:
        identity = current_identity()
        target = self._target(identity, request.target_tenant_id)
        scope = _scope(request.scope)
        async with self._engine.begin() as connection:
            decision = await self._decide(connection, identity, request.permission, target, scope)
            actor = identity.user_id or identity.subject
            if not decision.allowed:
                await audit(
                    connection,
                    tenant_id=identity.tenant_id,
                    action="authorization.denied",
                    actor_id=actor,
                    resource_type="permission",
                    resource_id=request.permission[:_MAXIMUM_ID],
                    outcome="denied",
                )
            elif decision.break_glass:
                # In the tenant whose data it is, so the tenant can see it (RBAC-003).
                await audit(
                    connection,
                    tenant_id=target,
                    action="authorization.break_glass.used",
                    actor_id=actor,
                    resource_type="permission",
                    resource_id=request.permission,
                    outcome="succeeded",
                )
        return authorization_pb2.CheckResponse(
            allowed=decision.allowed, reason=decision.reason, break_glass=decision.break_glass
        )

    async def GrantRole(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.GrantRoleRequest, context: Any
    ) -> authorization_pb2.GrantRoleResponse:
        _, actor, tenant = await self._require(
            "roles.manage", request.target_tenant_id, step_up=True
        )
        scope = _scope(request.scope)
        if not request.grantee_id or len(request.grantee_id) > _MAXIMUM_ID:
            raise InvalidRequestError("A user ID is required")
        try:
            policy.check_grantable(tenant, request.role, scope)
        except RuleError as error:
            raise InvalidRequestError(str(error)) from error
        now = _now()
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection, actor, "GrantRole", request, authorization_pb2.GrantRoleResponse
            )
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            if await find_active_grant(connection, tenant, request.grantee_id, request.role, scope):
                raise ConflictError("The user already has this role here")
            grant_id = await add_grant(
                connection, tenant, request.grantee_id, request.role, scope, actor=actor, now=now
            )
            await audit(
                connection,
                tenant_id=tenant,
                action=f"authorization.role.granted.{request.role}",
                actor_id=actor,
                resource_type="user",
                resource_id=request.grantee_id,
                outcome="succeeded",
            )
            row = (
                await connection.execute(
                    select(ROLE_GRANTS).where(ROLE_GRANTS.c.grant_id == grant_id)
                )
            ).one()
            response = authorization_pb2.GrantRoleResponse(grant=_grant_message(row))
            await remember(
                connection, actor, request.idempotency_key, "GrantRole", request, response, now
            )
        return response

    async def RevokeRole(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.RevokeRoleRequest, context: Any
    ) -> authorization_pb2.RevokeRoleResponse:
        _, actor, tenant = await self._require(
            "roles.manage", request.target_tenant_id, step_up=True
        )
        now = _now()
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection, actor, "RevokeRole", request, authorization_pb2.RevokeRoleResponse
            )
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            row = (
                await connection.execute(
                    select(ROLE_GRANTS).where(
                        ROLE_GRANTS.c.grant_id == request.grant_id,
                        ROLE_GRANTS.c.tenant_id == tenant,
                    )
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("No such grant in this tenant")
            if row.revoked_at is not None:
                raise ConflictError("The grant has already ended")
            keeps_tenant_running = (
                row.role
                in (
                    policy.TENANT_ADMINISTRATOR,
                    policy.PLATFORM_ADMINISTRATOR,
                )
                and Scope(row.wind_farm_id, row.turbine_id).whole_tenant
            )
            if keeps_tenant_running and not await other_whole_tenant_holders(
                connection, tenant, row.role, row.grant_id
            ):
                raise ConflictError("This would leave nobody able to manage roles")
            await end_grant(connection, row.grant_id, actor=actor, now=now)
            await audit(
                connection,
                tenant_id=tenant,
                action=f"authorization.role.revoked.{row.role}",
                actor_id=actor,
                resource_type="user",
                resource_id=row.user_id,
                outcome="succeeded",
            )
            ended = (
                await connection.execute(
                    select(ROLE_GRANTS).where(ROLE_GRANTS.c.grant_id == row.grant_id)
                )
            ).one()
            response = authorization_pb2.RevokeRoleResponse(grant=_grant_message(ended))
            await remember(
                connection, actor, request.idempotency_key, "RevokeRole", request, response, now
            )
        return response

    async def ListRoleGrants(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.ListRoleGrantsRequest, context: Any
    ) -> authorization_pb2.ListRoleGrantsResponse:
        _, _, tenant = await self._require("roles.manage", request.target_tenant_id, step_up=False)
        query = select(ROLE_GRANTS).where(ROLE_GRANTS.c.tenant_id == tenant)
        if request.grantee_id:
            query = query.where(ROLE_GRANTS.c.user_id == request.grantee_id)
        if not request.include_revoked:
            query = query.where(ROLE_GRANTS.c.revoked_at.is_(None))
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query.order_by(ROLE_GRANTS.c.granted_at))).all()
        return authorization_pb2.ListRoleGrantsResponse(grants=[_grant_message(r) for r in rows])

    async def GetRolePermissions(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.GetRolePermissionsRequest, context: Any
    ) -> authorization_pb2.GetRolePermissionsResponse:
        _, _, tenant = await self._require(
            "permissions.manage", request.target_tenant_id, step_up=False
        )
        if request.role not in policy.ROLES:
            raise InvalidRequestError("Unknown role")
        async with self._engine.connect() as connection:
            custom = await custom_mapping(connection, tenant)
        return authorization_pb2.GetRolePermissionsResponse(
            role_permissions=authorization_pb2.RolePermissions(
                tenant_id=tenant,
                role=request.role,
                permissions=sorted(policy.permissions_of(request.role, custom)),
                customized=custom is not None,
            )
        )

    async def SetRolePermissions(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.SetRolePermissionsRequest, context: Any
    ) -> authorization_pb2.SetRolePermissionsResponse:
        _, actor, tenant = await self._require(
            "permissions.manage", request.target_tenant_id, step_up=True
        )
        if tenant == policy.PLATFORM_TENANT:
            raise InvalidRequestError("The platform tenant has no tenant roles to change")
        try:
            chosen = policy.check_mapping(request.role, request.permissions)
        except RuleError as error:
            raise InvalidRequestError(str(error)) from error
        now = _now()
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection,
                actor,
                "SetRolePermissions",
                request,
                authorization_pb2.SetRolePermissionsResponse,
            )
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            await replace_role_permissions(
                connection, tenant, request.role, chosen, actor=actor, now=now
            )
            await audit(
                connection,
                tenant_id=tenant,
                action="authorization.role_permissions.changed",
                actor_id=actor,
                resource_type="role",
                resource_id=request.role,
                outcome="succeeded",
            )
            response = authorization_pb2.SetRolePermissionsResponse(
                role_permissions=authorization_pb2.RolePermissions(
                    tenant_id=tenant, role=request.role, permissions=sorted(chosen), customized=True
                )
            )
            await remember(
                connection,
                actor,
                request.idempotency_key,
                "SetRolePermissions",
                request,
                response,
                now,
            )
        return response

    async def StartBreakGlass(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.StartBreakGlassRequest, context: Any
    ) -> authorization_pb2.StartBreakGlassResponse:
        identity = current_identity()
        if not request.target_tenant_id:
            raise InvalidRequestError("Name the tenant to access")
        target = self._target(identity, request.target_tenant_id)
        user = self._user(identity)
        async with self._engine.connect() as connection:
            platform = await active_grants(connection, policy.PLATFORM_TENANT, user)
        is_platform_admin = identity.tenant_id == policy.PLATFORM_TENANT and any(
            grant.role == policy.PLATFORM_ADMINISTRATOR for grant in platform
        )
        if not is_platform_admin:
            await self._refuse(identity, "break_glass", "Only a Platform Administrator can do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError:
            await self._refuse(identity, "break_glass", STEP_UP)
        try:
            minutes = policy.check_break_glass(target, request.reason, request.duration_minutes)
        except RuleError as error:
            raise InvalidRequestError(str(error)) from error
        now = _now()
        grant_id = new_id()
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection,
                user,
                "StartBreakGlass",
                request,
                authorization_pb2.StartBreakGlassResponse,
            )
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            if await break_glass_until(connection, target, user, now) is not None:
                raise ConflictError("You already have break-glass access to this tenant")
            await connection.execute(
                BREAK_GLASS_GRANTS.insert().values(
                    grant_id=grant_id,
                    tenant_id=target,
                    user_id=user,
                    reason=request.reason.strip(),
                    started_at=now,
                    expires_at=now + timedelta(minutes=minutes),
                )
            )
            await audit(
                connection,
                tenant_id=target,
                action="authorization.break_glass.started",
                actor_id=user,
                resource_type="break_glass_grant",
                resource_id=grant_id,
                outcome="succeeded",
            )
            row = (
                await connection.execute(
                    select(BREAK_GLASS_GRANTS).where(BREAK_GLASS_GRANTS.c.grant_id == grant_id)
                )
            ).one()
            response = authorization_pb2.StartBreakGlassResponse(grant=_break_glass_message(row))
            await remember(
                connection, user, request.idempotency_key, "StartBreakGlass", request, response, now
            )
        return response

    async def EndBreakGlass(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.EndBreakGlassRequest, context: Any
    ) -> authorization_pb2.EndBreakGlassResponse:
        identity = current_identity()
        user = self._user(identity)
        now = _now()
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection, user, "EndBreakGlass", request, authorization_pb2.EndBreakGlassResponse
            )
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            row = (
                await connection.execute(
                    select(BREAK_GLASS_GRANTS).where(
                        BREAK_GLASS_GRANTS.c.grant_id == request.grant_id,
                        BREAK_GLASS_GRANTS.c.user_id == user,
                    )
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("No such break-glass grant of yours")
            if row.ended_at is not None or aware(row.expires_at) <= now:
                raise ConflictError("The grant has already ended")
            await connection.execute(
                update(BREAK_GLASS_GRANTS)
                .where(BREAK_GLASS_GRANTS.c.grant_id == row.grant_id)
                .values(ended_by=user, ended_at=now)
            )
            await audit(
                connection,
                tenant_id=row.tenant_id,
                action="authorization.break_glass.ended",
                actor_id=user,
                resource_type="break_glass_grant",
                resource_id=row.grant_id,
                outcome="succeeded",
            )
            ended = (
                await connection.execute(
                    select(BREAK_GLASS_GRANTS).where(BREAK_GLASS_GRANTS.c.grant_id == row.grant_id)
                )
            ).one()
            response = authorization_pb2.EndBreakGlassResponse(grant=_break_glass_message(ended))
            await remember(
                connection, user, request.idempotency_key, "EndBreakGlass", request, response, now
            )
        return response
