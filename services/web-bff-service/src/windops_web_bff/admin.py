"""The administrator console's API (SRS 9.4). It decides nothing: each service checks the caller.

Identity needs users.manage, Authorization roles.manage or permissions.manage, and Audit audit.read.
Every change also needs a recent second factor, which comes back as a step-up challenge.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from windops.audit.v1 import audit_pb2
from windops.authorization.v1 import authorization_pb2
from windops.identity.v1 import identity_pb2

from windops_web_bff.common import ID_PATTERN as _ID
from windops_web_bff.common import Call, Key, MemberId
from windops_web_bff.common import time_of as _time
from windops_web_bff.tokens import Caller

DENIED = "authorization.denied"


class User(BaseModel):
    member_id: str
    email: str
    first_name: str
    last_name: str
    enabled: bool
    created_at: str | None


class UserPage(BaseModel):
    users: list[User]
    next_page_token: str


class NewUser(BaseModel):
    email: str = Field(max_length=254)
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)


class CreatedUser(BaseModel):
    user: User
    # Shown once, in the first answer only. A retry with the same key has none.
    temporary_password: str | None
    already_created: bool


class Session(BaseModel):
    session_id: str
    ip_address: str
    started_at: str | None
    last_access_at: str | None
    clients: list[str]


class Sessions(BaseModel):
    sessions: list[Session]


class SessionsEnded(BaseModel):
    sessions_ended: int


class MfaStatus(BaseModel):
    otp_configured: bool
    recovery_codes_configured: bool
    setup_required: bool


class RoleGrant(BaseModel):
    grant_id: str
    user_id: str
    role: str
    # Both empty means the whole tenant.
    wind_farm_id: str
    turbine_id: str
    granted_by: str
    granted_at: str | None
    revoked_by: str
    revoked_at: str | None


class RoleGrants(BaseModel):
    grants: list[RoleGrant]


class NewGrant(BaseModel):
    user_id: str = Field(pattern=_ID)
    role: str = Field(pattern=_ID)
    wind_farm_id: str = Field(default="", max_length=64)
    turbine_id: str = Field(default="", max_length=64)


class RolePermissions(BaseModel):
    role: str
    permissions: list[str]
    # False while the tenant uses the SRS 3.2 defaults.
    customized: bool


class PermissionList(BaseModel):
    permissions: list[str] = Field(max_length=100)


class AccessDenial(BaseModel):
    sequence: int
    occurred_at: str | None
    user_id: str
    permission: str


class AccessDenials(BaseModel):
    denials: list[AccessDenial]
    next_page_token: str


def _user(message: Any) -> User:
    return User(
        member_id=message.member_id,
        email=message.email,
        first_name=message.first_name,
        last_name=message.last_name,
        enabled=message.enabled,
        created_at=_time(message, "created_at"),
    )


def _grant(message: Any) -> RoleGrant:
    return RoleGrant(
        grant_id=message.grant_id,
        user_id=message.user_id,
        role=message.role,
        wind_farm_id=message.scope.wind_farm_id,
        turbine_id=message.scope.turbine_id,
        granted_by=message.granted_by,
        granted_at=_time(message, "granted_at"),
        revoked_by=message.revoked_by,
        revoked_at=_time(message, "revoked_at"),
    )


def _role_permissions(message: Any) -> RolePermissions:
    return RolePermissions(
        role=message.role, permissions=list(message.permissions), customized=message.customized
    )


def add_admin_routes(router: APIRouter, caller: Any, call: Call) -> None:
    """Add the routes to the versioned router."""
    Who = Annotated[Caller, Depends(caller)]  # noqa: N806  # A type, named as one.

    @router.get("/admin/users", response_model=UserPage, summary="The tenant's users")
    async def list_users(
        who: Who,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(max_length=256)] = "",
    ) -> UserPage:
        answer = await call(
            "identity",
            "ListUsers",
            identity_pb2.ListUsersRequest(page_size=page_size, page_token=page_token),
            who,
        )
        return UserPage(
            users=[_user(item) for item in answer.users], next_page_token=answer.next_page_token
        )

    @router.post(
        "/admin/users",
        response_model=CreatedUser,
        status_code=201,
        summary="Add a user, who sets their own password at first login",
    )
    async def create_user(who: Who, body: NewUser, key: Key) -> CreatedUser:
        request = identity_pb2.CreateUserRequest(
            email=body.email,
            first_name=body.first_name,
            last_name=body.last_name,
            idempotency_key=key,
        )
        # Never retried here: a retry would answer without the password. The browser may resend.
        answer = await call("identity", "CreateUser", request, who, retry=False)
        return CreatedUser(
            user=_user(answer.user),
            temporary_password=answer.temporary_password or None,
            already_created=answer.already_created,
        )

    @router.get("/admin/users/{member_id}", response_model=User, summary="One user")
    async def get_user(who: Who, member_id: MemberId) -> User:
        answer = await call(
            "identity", "GetUser", identity_pb2.GetUserRequest(member_id=member_id), who
        )
        return _user(answer.user)

    @router.post(
        "/admin/users/{member_id}/disable",
        response_model=User,
        summary="Disable a user, which also ends their sessions",
    )
    async def disable_user(who: Who, member_id: MemberId, key: Key) -> User:
        request = identity_pb2.DisableUserRequest(member_id=member_id, idempotency_key=key)
        answer = await call("identity", "DisableUser", request, who, retry=False)
        return _user(answer.user)

    @router.post(
        "/admin/users/{member_id}/reactivate", response_model=User, summary="Reactivate a user"
    )
    async def reactivate_user(who: Who, member_id: MemberId, key: Key) -> User:
        request = identity_pb2.ReactivateUserRequest(member_id=member_id, idempotency_key=key)
        answer = await call("identity", "ReactivateUser", request, who, retry=False)
        return _user(answer.user)

    @router.get(
        "/admin/users/{member_id}/sessions", response_model=Sessions, summary="A user's sessions"
    )
    async def list_sessions(who: Who, member_id: MemberId) -> Sessions:
        answer = await call(
            "identity", "ListSessions", identity_pb2.ListSessionsRequest(member_id=member_id), who
        )
        return Sessions(
            sessions=[
                Session(
                    session_id=item.session_id,
                    ip_address=item.ip_address,
                    started_at=_time(item, "started_at"),
                    last_access_at=_time(item, "last_access_at"),
                    clients=list(item.clients),
                )
                for item in answer.sessions
            ]
        )

    @router.post(
        "/admin/users/{member_id}/sessions/revoke",
        response_model=SessionsEnded,
        summary="End every session a user has",
    )
    async def revoke_sessions(who: Who, member_id: MemberId, key: Key) -> SessionsEnded:
        request = identity_pb2.RevokeSessionsRequest(member_id=member_id, idempotency_key=key)
        answer = await call("identity", "RevokeSessions", request, who, retry=False)
        return SessionsEnded(sessions_ended=answer.sessions_ended)

    @router.get(
        "/admin/users/{member_id}/mfa",
        response_model=MfaStatus,
        summary="Whether a user has set up a second factor",
    )
    async def mfa_status(who: Who, member_id: MemberId) -> MfaStatus:
        answer = await call(
            "identity", "GetMfaStatus", identity_pb2.GetMfaStatusRequest(member_id=member_id), who
        )
        return MfaStatus(
            otp_configured=answer.otp_configured,
            recovery_codes_configured=answer.recovery_codes_configured,
            setup_required=answer.setup_required,
        )

    @router.get(
        "/admin/role-grants", response_model=RoleGrants, summary="Who holds which role, and where"
    )
    async def list_grants(
        who: Who,
        user_id: Annotated[str, Query(max_length=64)] = "",
        include_revoked: bool = False,
    ) -> RoleGrants:
        request = authorization_pb2.ListRoleGrantsRequest(
            grantee_id=user_id, include_revoked=include_revoked
        )
        answer = await call("authorization", "ListRoleGrants", request, who)
        return RoleGrants(grants=[_grant(item) for item in answer.grants])

    @router.post(
        "/admin/role-grants",
        response_model=RoleGrant,
        status_code=201,
        summary="Give a user a role, in the whole tenant, a wind farm or a turbine",
    )
    async def grant_role(who: Who, body: NewGrant, key: Key) -> RoleGrant:
        request = authorization_pb2.GrantRoleRequest(
            grantee_id=body.user_id,
            role=body.role,
            scope=authorization_pb2.Scope(
                wind_farm_id=body.wind_farm_id, turbine_id=body.turbine_id
            ),
            idempotency_key=key,
        )
        answer = await call("authorization", "GrantRole", request, who, retry=False)
        return _grant(answer.grant)

    @router.post(
        "/admin/role-grants/{grant_id}/revoke",
        response_model=RoleGrant,
        summary="End a grant. It is kept, marked as ended",
    )
    async def revoke_role(
        who: Who, grant_id: Annotated[str, Path(pattern=_ID)], key: Key
    ) -> RoleGrant:
        request = authorization_pb2.RevokeRoleRequest(grant_id=grant_id, idempotency_key=key)
        answer = await call("authorization", "RevokeRole", request, who, retry=False)
        return _grant(answer.grant)

    @router.get(
        "/admin/roles/{role}/permissions",
        response_model=RolePermissions,
        summary="The permissions a role gives in this tenant",
    )
    async def role_permissions(
        who: Who, role: Annotated[str, Path(pattern=_ID)]
    ) -> RolePermissions:
        answer = await call(
            "authorization",
            "GetRolePermissions",
            authorization_pb2.GetRolePermissionsRequest(role=role),
            who,
        )
        return _role_permissions(answer.role_permissions)

    @router.put(
        "/admin/roles/{role}/permissions",
        response_model=RolePermissions,
        summary="Replace the permissions a role gives in this tenant",
    )
    async def set_role_permissions(
        who: Who, role: Annotated[str, Path(pattern=_ID)], body: PermissionList, key: Key
    ) -> RolePermissions:
        request = authorization_pb2.SetRolePermissionsRequest(
            role=role, permissions=body.permissions, idempotency_key=key
        )
        answer = await call("authorization", "SetRolePermissions", request, who, retry=False)
        return _role_permissions(answer.role_permissions)

    @router.get(
        "/admin/access-denials",
        response_model=AccessDenials,
        summary="Refused attempts in this tenant, oldest first, from the audit trail",
    )
    async def access_denials(
        who: Who,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(max_length=32)] = "",
    ) -> AccessDenials:
        request = audit_pb2.ListAuditRecordsRequest(
            page_size=page_size, page_token=page_token, action=DENIED
        )
        answer = await call("audit", "ListAuditRecords", request, who)
        return AccessDenials(
            denials=[
                AccessDenial(
                    sequence=item.sequence,
                    occurred_at=_time(item, "occurred_at"),
                    user_id=item.actor_id,
                    permission=item.resource_id,
                )
                for item in answer.records
            ],
            next_page_token=answer.next_page_token,
        )
