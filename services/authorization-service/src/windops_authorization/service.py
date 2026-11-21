"""The gRPC API. Every rule lives in policy.py, and this checks who is asking before using it."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.authorization.v1 import authorization_pb2, authorization_pb2_grpc
from windops_auth import (
    STEP_UP_MESSAGE,
    MfaRequiredError,
    authentication_claims,
    require_recent_mfa,
)
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    VerifiedIdentity,
    current_identity,
)
from windops_migrations import across_tenants, in_tenant

from windops_authorization import changes, policy
from windops_authorization.policy import Decision, RuleError, Scope
from windops_authorization.store import (
    BREAK_GLASS_GRANTS,
    PENDING_CHANGES,
    ROLE_GRANTS,
    KeyReusedError,
    active_grants,
    audit,
    aware,
    break_glass_until,
    custom_mapping,
    find_active_grant,
    mapping_without_own_changes,
    new_id,
    permissions_version,
    remember,
    remembered,
    tenant_administrators,
)

_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_MAXIMUM_ID = 255
_MAXIMUM_KEY = 128
# Each lasts at most 4 hours and one tenant has one at a time, so this is never reached in use.
_MAXIMUM_LISTED = 200
STEP_UP = STEP_UP_MESSAGE


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
        eligible_since: datetime | None = None,
    ) -> Decision:
        if identity.actor:
            # An agent never does more than read and draft, whoever it acts for (SEC-009).
            refused = policy.decide_for_agent(
                identity.actor, permission, tenant_id, identity.tenant_id
            )
            if refused is not None:
                return refused
        if not identity.user_id:
            return policy.decide_for_service(
                policy.service_of(identity.subject), permission, tenant_id, identity.tenant_id
            )
        if tenant_id == identity.tenant_id:
            custom = await custom_mapping(connection, tenant_id)
            decision = policy.decide_in_tenant(
                permission,
                scope,
                await active_grants(connection, tenant_id, identity.user_id),
                custom,
            )
            if not decision.allowed or eligible_since is None:
                return decision
            # RBAC-006: allowed also without what the caller changed themselves since then.
            without = policy.decide_in_tenant(
                permission, scope,
                await active_grants(connection, tenant_id, identity.user_id,
                                    not_self_after=eligible_since),
                await mapping_without_own_changes(connection, tenant_id, identity.user_id,
                                                  eligible_since, custom),
            )  # fmt: skip
            if without.allowed:
                return decision
            return Decision(False, "Only a change you made yourself after the request allows "
                                   "this, so it does not count (RBAC-006)")  # fmt: skip
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
        since = (aware(request.eligible_since.ToDatetime())
                 if request.HasField("eligible_since") else None)  # fmt: skip
        async with self._engine.begin() as connection:
            decision = await self._decide(
                connection, identity, request.permission, target, scope, since
            )
            version = await permissions_version(connection, target)
            actor = identity.user_id or identity.subject
            if not decision.allowed:
                # An agent is the actor, on its principal's behalf (SEC-010).
                await audit(
                    connection,
                    tenant_id=identity.tenant_id,
                    action="authorization.denied",
                    actor_id=identity.actor or actor,
                    actor_type="agent" if identity.actor else "user",
                    on_behalf_of=actor if identity.actor else "",
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
            allowed=decision.allowed, reason=decision.reason, break_glass=decision.break_glass,
            permissions_version=version,
        )  # fmt: skip

    async def ListMyPermissions(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.ListMyPermissionsRequest, context: Any
    ) -> authorization_pb2.ListMyPermissionsResponse:
        identity = current_identity()
        # An agent sees only what it may use: reads and drafts (SEC-009).
        usable = policy.AGENT_PERMISSIONS if identity.actor else policy.PERMISSIONS
        if not identity.user_id:
            # A service's fixed grants, tenant-wide in its own tenant (ADR 0008).
            service = policy.service_of(identity.subject)
            granted = policy.SERVICE_GRANTS.get(service or "", frozenset())
            return authorization_pb2.ListMyPermissionsResponse(
                permissions=[
                    authorization_pb2.ScopedPermission(permission=permission)
                    for permission in sorted(granted & usable)
                ]
            )
        async with self._engine.connect() as connection:
            grants = await active_grants(connection, identity.tenant_id, identity.user_id)
            custom = await custom_mapping(connection, identity.tenant_id)
        seen: set[tuple[str, str, str]] = set()
        permissions = []
        for grant in grants:
            for permission in sorted(policy.permissions_of(grant.role, custom) & usable):
                key = (permission, grant.scope.wind_farm_id, grant.scope.turbine_id)
                if key not in seen:
                    seen.add(key)
                    permissions.append(
                        authorization_pb2.ScopedPermission(
                            permission=permission,
                            scope=authorization_pb2.Scope(
                                wind_farm_id=grant.scope.wind_farm_id,
                                turbine_id=grant.scope.turbine_id,
                            ),
                        )
                    )
        return authorization_pb2.ListMyPermissionsResponse(
            roles=[
                authorization_pb2.ScopedRole(
                    role=grant.role,
                    scope=authorization_pb2.Scope(
                        wind_farm_id=grant.scope.wind_farm_id, turbine_id=grant.scope.turbine_id
                    ),
                )
                for grant in grants
            ],
            permissions=permissions,
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
            if changes.role_approves(request.role, await custom_mapping(connection, tenant)):
                # It changes who may approve: a second person confirms it (RBAC-007).
                held = await changes.propose(
                    connection, tenant, changes.GRANT, request, role=request.role, actor=actor,
                    now=now, grantee_id=request.grantee_id,
                    summary=f"Give {request.grantee_id} the {request.role} role, in "
                            f"{changes.where(scope)}",
                )  # fmt: skip
                response = authorization_pb2.GrantRoleResponse(pending=held)
            else:
                response = authorization_pb2.GrantRoleResponse(
                    grant=await changes.apply_grant(
                        connection, tenant, request, actor=actor, now=now
                    )
                )
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
            row = await changes.active_grant(connection, tenant, request.grant_id)
            if changes.role_approves(row.role, await custom_mapping(connection, tenant)):
                held = await changes.propose(
                    connection, tenant, changes.REVOKE, request, role=row.role, actor=actor,
                    now=now, grantee_id=row.user_id, grant_id=row.grant_id,
                    summary=f"End {row.user_id}'s {row.role} role, in "
                            f"{changes.where(Scope(row.wind_farm_id, row.turbine_id))}",
                )  # fmt: skip
                response = authorization_pb2.RevokeRoleResponse(pending=held)
            else:
                response = authorization_pb2.RevokeRoleResponse(
                    grant=await changes.apply_revoke(
                        connection, tenant, row.grant_id, actor=actor, now=now
                    )
                )
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
            await in_tenant(connection, tenant)
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
            custom = await custom_mapping(connection, tenant)
            if changes.changes_approval(request.role, custom, chosen):
                verb = "Take" if changes.role_approves(request.role, custom) else "Give"
                held = await changes.propose(
                    connection, tenant, changes.ROLE_PERMISSIONS, request, role=request.role,
                    actor=actor, now=now, permissions=chosen,
                    summary=f"{verb} {changes.APPROVE} {'from' if verb == 'Take' else 'to'} the "
                            f"{request.role} role, with {len(chosen)} permissions in all",
                )  # fmt: skip
                response = authorization_pb2.SetRolePermissionsResponse(pending=held)
            else:
                response = authorization_pb2.SetRolePermissionsResponse(
                    role_permissions=await changes.apply_role_permissions(
                        connection, tenant, request.role, chosen, actor=actor, now=now
                    )
                )
            await remember(
                connection, actor, request.idempotency_key, "SetRolePermissions", request,
                response, now,
            )  # fmt: skip
        return response

    # --- Approval policy changes, confirmed by a second person (RBAC-007)

    async def _may_decide_change(self, identity: VerifiedIdentity, user: str, row: Any) -> None:
        """A second Tenant Administrator with the change's permission, or, where the tenant has
        no other, a Platform Administrator. Each with a recent second factor."""
        permission = (
            "permissions.manage" if row.kind == changes.ROLE_PERMISSIONS else "roles.manage"
        )
        if identity.tenant_id == row.tenant_id:
            await self._require(permission, row.tenant_id, step_up=True)
            return
        await self._require_platform_administrator(identity, user)
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError:
            await self._refuse(identity, permission, STEP_UP)
        async with self._engine.connect() as connection:
            others = await tenant_administrators(connection, row.tenant_id, besides=row.proposed_by)
        if others:
            await self._refuse(identity, permission, "This tenant has another Tenant "
                               "Administrator to confirm it, so it is not escalated")  # fmt: skip

    async def _pending(self, tenant: str, change_id: str, now: datetime) -> Any:
        async with self._engine.connect() as connection:
            row = await changes.load(connection, tenant, change_id)
        status = changes.status_of(row, now)
        if status != changes.PENDING:
            raise ConflictError(f"The change is {status}, and can no longer be decided")
        return row

    async def ListPendingChanges(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.ListPendingChangesRequest, context: Any
    ) -> authorization_pb2.ListPendingChangesResponse:
        identity, now = current_identity(), _now()
        c = PENDING_CHANGES.c
        query = select(PENDING_CHANGES).order_by(c.proposed_at.desc()).limit(_MAXIMUM_LISTED)
        if not request.include_decided:
            query = query.where(c.status == changes.PENDING)
        if identity.tenant_id == policy.PLATFORM_TENANT and not request.target_tenant_id:
            # The escalations, in every tenant, for a Platform Administrator.
            await self._require_platform_administrator(identity, self._user(identity))
            async with self._engine.connect() as connection:
                await across_tenants(connection)
                rows = (await connection.execute(query.where(c.escalated.is_(True)))).all()
        else:
            _, _, tenant = await self._require(
                "roles.manage", request.target_tenant_id, step_up=False
            )
            async with self._engine.connect() as connection:
                await in_tenant(connection, tenant)
                rows = (await connection.execute(query.where(c.tenant_id == tenant))).all()
        return authorization_pb2.ListPendingChangesResponse(
            changes=[changes.change_message(r, now) for r in rows]
        )

    async def ConfirmChange(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.ConfirmChangeRequest, context: Any
    ) -> authorization_pb2.ConfirmChangeResponse:
        identity = current_identity()
        user, tenant, now = (
            self._user(identity),
            self._target(identity, request.target_tenant_id),
            _now(),
        )
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection, user, "ConfirmChange", request, authorization_pb2.ConfirmChangeResponse
            )
        if replay is not None:
            return replay  # type: ignore[no-any-return]
        row = await self._pending(tenant, request.change_id, now)
        if user == row.proposed_by:
            await self._refuse(identity, "approval_policy.confirm", "A second administrator must "
                               "confirm a change to who may approve (RBAC-007)")  # fmt: skip
        await self._may_decide_change(identity, user, row)
        async with self._engine.begin() as connection:
            row = await changes.load(connection, tenant, request.change_id)
            if changes.status_of(row, now) != changes.PENDING:
                raise ConflictError("Someone else decided it first")
            response = authorization_pb2.ConfirmChangeResponse()
            # Applied as the proposer's change, so it never makes them eligible for an earlier
            # approval (RBAC-006).
            if row.kind == changes.GRANT:
                response.grant.CopyFrom(await changes.apply_grant(
                    connection, tenant, authorization_pb2.GrantRoleRequest.FromString(row.request),
                    actor=row.proposed_by, now=now))  # fmt: skip
            elif row.kind == changes.REVOKE:
                response.grant.CopyFrom(
                    await changes.apply_revoke(
                        connection, tenant, row.grant_id, actor=row.proposed_by, now=now
                    )
                )
            else:
                chosen = frozenset(p for p in row.permissions.split(",") if p)
                response.role_permissions.CopyFrom(
                    await changes.apply_role_permissions(
                        connection, tenant, row.role, chosen, actor=row.proposed_by, now=now
                    )
                )
            await self._decide_change(connection, row, changes.CONFIRMED, user, now)
            response.change.CopyFrom(
                changes.change_message(await changes.load(connection, tenant, row.change_id), now)
            )
            await audit(connection, tenant_id=tenant,
                        action="authorization.approval_policy.confirmed", actor_id=user,
                        resource_type="pending_change", resource_id=row.change_id,
                        outcome="succeeded")  # fmt: skip
            await remember(connection, user, request.idempotency_key, "ConfirmChange", request,
                           response, now)  # fmt: skip
        return response

    async def DeclineChange(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.DeclineChangeRequest, context: Any
    ) -> authorization_pb2.DeclineChangeResponse:
        identity = current_identity()
        user, tenant, now = (
            self._user(identity),
            self._target(identity, request.target_tenant_id),
            _now(),
        )
        reason = request.reason.strip()
        if not reason or len(reason) > 500:
            raise InvalidRequestError("A reason of at most 500 characters is needed")
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection, user, "DeclineChange", request, authorization_pb2.DeclineChangeResponse
            )
        if replay is not None:
            return replay  # type: ignore[no-any-return]
        row = await self._pending(tenant, request.change_id, now)
        if user != row.proposed_by:
            # The proposer withdraws their own; anyone else must be able to confirm it.
            await self._may_decide_change(identity, user, row)
        async with self._engine.begin() as connection:
            row = await changes.load(connection, tenant, request.change_id)
            if changes.status_of(row, now) != changes.PENDING:
                raise ConflictError("Someone else decided it first")
            await self._decide_change(connection, row, changes.DECLINED, user, now, reason)
            response = authorization_pb2.DeclineChangeResponse(change=changes.change_message(
                await changes.load(connection, tenant, row.change_id), now))  # fmt: skip
            await audit(connection, tenant_id=tenant,
                        action="authorization.approval_policy.declined", actor_id=user,
                        resource_type="pending_change", resource_id=row.change_id,
                        outcome="succeeded")  # fmt: skip
            await remember(connection, user, request.idempotency_key, "DeclineChange", request,
                           response, now)  # fmt: skip
        return response

    @staticmethod
    async def _decide_change(connection: AsyncConnection, row: Any, status: str, user: str,
                             now: datetime, reason: str = "") -> None:  # fmt: skip
        c = PENDING_CHANGES.c
        await connection.execute(
            update(PENDING_CHANGES)
            .where(c.change_id == row.change_id, c.status == changes.PENDING)
            .values(status=status, decided_by=user, decided_at=now, decline_reason=reason)
        )

    async def StartBreakGlass(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.StartBreakGlassRequest, context: Any
    ) -> authorization_pb2.StartBreakGlassResponse:
        identity = current_identity()
        if not request.target_tenant_id:
            raise InvalidRequestError("Name the tenant to access")
        target = self._target(identity, request.target_tenant_id)
        user = self._user(identity)
        await self._require_platform_administrator(identity, user)
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
            await in_tenant(connection, target)
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

    async def _require_platform_administrator(self, identity: VerifiedIdentity, user: str) -> None:
        """Break-glass is only for a Platform Administrator, signed in to the platform tenant."""
        async with self._engine.connect() as connection:
            platform = await active_grants(connection, policy.PLATFORM_TENANT, user)
        if not (
            identity.tenant_id == policy.PLATFORM_TENANT
            and any(grant.role == policy.PLATFORM_ADMINISTRATOR for grant in platform)
        ):
            await self._refuse(identity, "break_glass", "Only a Platform Administrator can do this")

    async def ListMyBreakGlassGrants(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.ListMyBreakGlassGrantsRequest, context: Any
    ) -> authorization_pb2.ListMyBreakGlassGrantsResponse:
        identity = current_identity()
        user = self._user(identity)
        # Nobody else holds any, so anyone else is refused, rather than shown an empty list.
        await self._require_platform_administrator(identity, user)
        now = _now()
        async with self._engine.begin() as connection:
            # The caller's own grants, wherever they are. Nobody else's can be named here.
            await across_tenants(connection)
            rows = (
                await connection.execute(
                    select(BREAK_GLASS_GRANTS)
                    .where(
                        BREAK_GLASS_GRANTS.c.user_id == user,
                        BREAK_GLASS_GRANTS.c.ended_at.is_(None),
                        BREAK_GLASS_GRANTS.c.expires_at > now,
                    )
                    .order_by(BREAK_GLASS_GRANTS.c.expires_at)
                    .limit(_MAXIMUM_LISTED)
                )
            ).all()
        return authorization_pb2.ListMyBreakGlassGrantsResponse(
            grants=[_break_glass_message(row) for row in rows]
        )

    async def EndBreakGlass(  # noqa: N802  # The name comes from the contract.
        self, request: authorization_pb2.EndBreakGlassRequest, context: Any
    ) -> authorization_pb2.EndBreakGlassResponse:
        identity = current_identity()
        user = self._user(identity)
        await self._require_platform_administrator(identity, user)
        now = _now()
        async with self._engine.begin() as connection:
            replay = await _replayed(
                connection, user, "EndBreakGlass", request, authorization_pb2.EndBreakGlassResponse
            )
            if replay is not None:
                return replay  # type: ignore[no-any-return]
            # The request names only the grant, so find it among the caller's own, in any tenant.
            await across_tenants(connection)
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
            # Back to the one tenant, which also ends the read across tenants.
            await in_tenant(connection, row.tenant_id)
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
