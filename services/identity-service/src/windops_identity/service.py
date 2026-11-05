"""The gRPC API. It asks Authorization first, then checks the tenant on every user it touches."""

import logging
import re
import secrets
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.authorization.v1 import authorization_pb2
from windops.identity.v1 import identity_pb2, identity_pb2_grpc
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
    ServiceClient,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
)
from windops_resilience import BreakerOpenError

from windops_identity.keycloak import KeycloakAdmin, KeycloakError
from windops_identity.store import KeyReusedError, audit, created_before, record_created

logger = logging.getLogger("windops.service")
PERMISSION = "users.manage"
TENANT_PERMISSION = "tenant.manage"
_MAXIMUM_TENANT_NAME = 200
STEP_UP = STEP_UP_MESSAGE
DEADLINE_SECONDS = 3.0
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
# Deliberately simple: Keycloak and the user's first login do the real check.
_EMAIL = re.compile(r"^[^@\s]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}$")
_MAXIMUM_NAME = 100
_MAXIMUM_ID = 64
_DEFAULT_PAGE, _MAXIMUM_PAGE = 50, 200
# The user must choose their own password, and set up a second factor if their role needs one.
FIRST_LOGIN_ACTIONS = ["UPDATE_PASSWORD"]


class Authorizer(Protocol):
    async def allowed(self, permission: str, target_tenant_id: str) -> bool: ...


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub = stub
        self._client = client

    async def allowed(self, permission: str, target_tenant_id: str) -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission, target_tenant_id=target_tenant_id
        )
        try:
            response = await self._client.call(
                self._stub.Check, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(response.allowed)


def strong_password() -> str:
    """A temporary password that always meets the realm's policy (SEC-004)."""
    return f"Wo1!{secrets.token_urlsafe(24)}"


def _timestamp_ms(value: Any) -> Timestamp | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    stamp = Timestamp()
    stamp.FromMilliseconds(value)
    return stamp


def _user(body: dict[str, Any]) -> identity_pb2.User:
    return identity_pb2.User(
        member_id=str(body["id"]),
        email=str(body.get("email", "")),
        first_name=str(body.get("firstName", "")),
        last_name=str(body.get("lastName", "")),
        enabled=bool(body.get("enabled", False)),
        created_at=_timestamp_ms(body.get("createdTimestamp")),
    )


def _session(body: dict[str, Any]) -> identity_pb2.Session:
    clients = body.get("clients") or {}
    return identity_pb2.Session(
        session_id=str(body.get("id", "")),
        ip_address=str(body.get("ipAddress", "")),
        started_at=_timestamp_ms(body.get("start")),
        last_access_at=_timestamp_ms(body.get("lastAccess")),
        clients=sorted(str(name) for name in clients.values()),
    )


class IdentityService(identity_pb2_grpc.IdentityServiceServicer):
    def __init__(
        self, engine: AsyncEngine, keycloak: KeycloakAdmin, authorizer: Authorizer
    ) -> None:
        self._engine = engine
        self._keycloak = keycloak
        self._authorizer = authorizer

    async def _allow(
        self, target_tenant_id: str, *, step_up: bool, permission: str = PERMISSION
    ) -> tuple[VerifiedIdentity, str]:
        """Return the caller and the tenant they act in, or refuse."""
        identity = current_identity()
        tenant = target_tenant_id or identity.tenant_id
        if not _TENANT.match(tenant):
            raise InvalidRequestError("The tenant ID is not valid")
        if not identity.user_id:
            raise PermissionDeniedError("Only a user can do this")
        # Authorization audits a denial, so it is not recorded twice here.
        target = "" if tenant == identity.tenant_id else tenant
        if not await self._authorizer.allowed(permission, target):
            raise PermissionDeniedError("You do not have permission to do this")
        if step_up:
            try:
                require_recent_mfa(authentication_claims(identity))
            except MfaRequiredError as error:
                raise PermissionDeniedError(STEP_UP) from error
        return identity, tenant

    async def _organization(self, tenant: str) -> str:
        organization = await self._keycloak.organization_id(tenant)
        if organization is None:
            raise NotFoundError("No such tenant")
        return organization

    async def _member(self, tenant: str, member_id: str) -> str:
        """Return the tenant's organization, if the user is in it. Otherwise they do not exist."""
        if not member_id or len(member_id) > _MAXIMUM_ID:
            raise InvalidRequestError("A member ID is required")
        organization = await self._organization(tenant)
        if not await self._keycloak.is_member(organization, member_id):
            raise NotFoundError("No such user in this tenant")
        return organization

    async def _guarded[T](self, work: Any) -> T:
        try:
            result: T = await work
        except (KeycloakError, BreakerOpenError) as error:
            status = getattr(error, "status", None)
            # The status only: a response body could hold a user's details.
            logger.warning(
                "Keycloak refused or failed a call",
                extra={"error_type": type(error).__name__, "keycloak_status": status},
            )
            if status == 409:
                raise ConflictError("Keycloak refused the change, because it conflicts") from error
            raise UnavailableError("User management is unavailable right now") from error
        return result

    async def CreateUser(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.CreateUserRequest, context: Any
    ) -> identity_pb2.CreateUserResponse:
        identity, tenant = await self._allow(request.target_tenant_id, step_up=True)
        actor = identity.user_id or ""
        email = request.email.strip().lower()
        if not _EMAIL.match(email):
            raise InvalidRequestError("A valid email address is required")
        for name in (request.first_name, request.last_name):
            if len(name) > _MAXIMUM_NAME:
                raise InvalidRequestError("A name is too long")
        if len(request.idempotency_key) > _MAXIMUM_ID:
            raise InvalidRequestError("The idempotency key is too long")
        async with self._engine.connect() as connection:
            try:
                existing = await created_before(
                    connection, tenant, actor, request.idempotency_key, request
                )
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
        if existing is not None:
            body: dict[str, Any] = await self._guarded(self._keycloak.user(existing))
            return identity_pb2.CreateUserResponse(user=_user(body), already_created=True)
        return await self._guarded(self._create(request, tenant, actor, email))

    async def _create(
        self, request: identity_pb2.CreateUserRequest, tenant: str, actor: str, email: str
    ) -> identity_pb2.CreateUserResponse:
        organization = await self._organization(tenant)
        if await self._keycloak.find_by_email(email):
            # The same answer whichever tenant has it, so this reveals no other tenant's users.
            raise ConflictError("This email address cannot be used")
        member = await self._keycloak.create_user(
            {
                "username": email,
                "email": email,
                "emailVerified": False,
                "enabled": True,
                "firstName": request.first_name,
                "lastName": request.last_name,
                "requiredActions": FIRST_LOGIN_ACTIONS,
            }
        )
        password = strong_password()
        try:
            await self._keycloak.set_temporary_password(member, password)
            await self._keycloak.add_member(organization, member)
        except KeycloakError:
            # A user in no tenant cannot sign in to anything, but must not be left behind.
            await self._keycloak.delete_user(member)
            raise
        async with self._engine.begin() as connection:
            await record_created(
                connection,
                actor=actor,
                key=request.idempotency_key,
                request=request,
                tenant_id=tenant,
                member_id=member,
                now=datetime.now(UTC),
            )
            await audit(
                connection,
                tenant_id=tenant,
                action="identity.user.created",
                actor_id=actor,
                member_id=member,
            )
        body = await self._keycloak.user(member)
        return identity_pb2.CreateUserResponse(user=_user(body), temporary_password=password)

    async def GetUser(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.GetUserRequest, context: Any
    ) -> identity_pb2.GetUserResponse:
        _, tenant = await self._allow(request.target_tenant_id, step_up=False)

        async def work() -> identity_pb2.GetUserResponse:
            await self._member(tenant, request.member_id)
            return identity_pb2.GetUserResponse(
                user=_user(await self._keycloak.user(request.member_id))
            )

        return await self._guarded(work())

    async def ListUsers(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.ListUsersRequest, context: Any
    ) -> identity_pb2.ListUsersResponse:
        _, tenant = await self._allow(request.target_tenant_id, step_up=False)
        size = request.page_size or _DEFAULT_PAGE
        if not 1 <= size <= _MAXIMUM_PAGE:
            raise InvalidRequestError(f"The page size must be from 1 to {_MAXIMUM_PAGE}")
        if request.page_token and not request.page_token.isdigit():
            raise InvalidRequestError("The page token is not valid")
        first = int(request.page_token or 0)

        async def work() -> identity_pb2.ListUsersResponse:
            organization = await self._organization(tenant)
            # One more than asked, to know whether another page exists.
            members = await self._keycloak.members(organization, first, size + 1)
            following = str(first + size) if len(members) > size else ""
            return identity_pb2.ListUsersResponse(
                users=[_user(item) for item in members[:size]], next_page_token=following
            )

        return await self._guarded(work())

    async def _set_enabled(
        self, target_tenant_id: str, member_id: str, enabled: bool
    ) -> identity_pb2.User:
        identity, tenant = await self._allow(target_tenant_id, step_up=True)
        if not enabled and member_id == identity.user_id:
            raise InvalidRequestError("You cannot disable your own account")

        async def work() -> identity_pb2.User:
            await self._member(tenant, member_id)
            before = await self._keycloak.user(member_id)
            if bool(before.get("enabled")) != enabled:
                await self._keycloak.set_enabled(member_id, enabled)
                if not enabled:
                    # A disabled user's refresh tokens must stop working at once (SEC-004).
                    await self._keycloak.logout(member_id)
                async with self._engine.begin() as connection:
                    await audit(
                        connection,
                        tenant_id=tenant,
                        action=f"identity.user.{'reactivated' if enabled else 'disabled'}",
                        actor_id=identity.user_id or "",
                        member_id=member_id,
                    )
            return _user(await self._keycloak.user(member_id))

        return await self._guarded(work())

    async def DisableUser(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.DisableUserRequest, context: Any
    ) -> identity_pb2.DisableUserResponse:
        user = await self._set_enabled(request.target_tenant_id, request.member_id, False)
        return identity_pb2.DisableUserResponse(user=user)

    async def ReactivateUser(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.ReactivateUserRequest, context: Any
    ) -> identity_pb2.ReactivateUserResponse:
        user = await self._set_enabled(request.target_tenant_id, request.member_id, True)
        return identity_pb2.ReactivateUserResponse(user=user)

    async def ListSessions(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.ListSessionsRequest, context: Any
    ) -> identity_pb2.ListSessionsResponse:
        _, tenant = await self._allow(request.target_tenant_id, step_up=False)

        async def work() -> identity_pb2.ListSessionsResponse:
            await self._member(tenant, request.member_id)
            sessions = await self._keycloak.sessions(request.member_id)
            return identity_pb2.ListSessionsResponse(sessions=[_session(s) for s in sessions])

        return await self._guarded(work())

    async def RevokeSessions(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.RevokeSessionsRequest, context: Any
    ) -> identity_pb2.RevokeSessionsResponse:
        identity, tenant = await self._allow(request.target_tenant_id, step_up=True)

        async def work() -> identity_pb2.RevokeSessionsResponse:
            await self._member(tenant, request.member_id)
            ended = len(await self._keycloak.sessions(request.member_id))
            if ended:
                await self._keycloak.logout(request.member_id)
                async with self._engine.begin() as connection:
                    await audit(
                        connection,
                        tenant_id=tenant,
                        action="identity.sessions.revoked",
                        actor_id=identity.user_id or "",
                        member_id=request.member_id,
                    )
            return identity_pb2.RevokeSessionsResponse(sessions_ended=ended)

        return await self._guarded(work())

    async def GetMfaStatus(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.GetMfaStatusRequest, context: Any
    ) -> identity_pb2.GetMfaStatusResponse:
        _, tenant = await self._allow(request.target_tenant_id, step_up=False)

        async def work() -> identity_pb2.GetMfaStatusResponse:
            await self._member(tenant, request.member_id)
            kinds = {
                str(item.get("type"))
                for item in await self._keycloak.credentials(request.member_id)
            }
            actions = set(
                (await self._keycloak.user(request.member_id)).get("requiredActions") or []
            )
            return identity_pb2.GetMfaStatusResponse(
                otp_configured="otp" in kinds,
                recovery_codes_configured="recovery-authn-codes" in kinds,
                setup_required=bool(actions & {"CONFIGURE_TOTP", "CONFIGURE_RECOVERY_AUTHN_CODES"}),
            )

        return await self._guarded(work())

    async def CreateOrganization(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.CreateOrganizationRequest, context: Any
    ) -> identity_pb2.CreateOrganizationResponse:
        identity, tenant = await self._allow(
            request.target_tenant_id, step_up=True, permission=TENANT_PERMISSION
        )
        name = request.name.strip()
        if not name or len(name) > _MAXIMUM_TENANT_NAME:
            raise InvalidRequestError("A tenant name is required")

        async def work() -> identity_pb2.CreateOrganizationResponse:
            existing = await self._keycloak.organization(tenant)
            if existing is not None:
                if existing.get("name") != name:
                    raise ConflictError("A tenant with this ID already exists")
                return identity_pb2.CreateOrganizationResponse(
                    organization_id=str(existing["id"]), created=False
                )
            organization = await self._keycloak.create_organization(tenant, name)
            async with self._engine.begin() as connection:
                await audit(
                    connection,
                    tenant_id=tenant,
                    action="identity.organization.created",
                    actor_id=identity.user_id or "",
                    member_id=organization,
                )
            return identity_pb2.CreateOrganizationResponse(
                organization_id=organization, created=True
            )

        return await self._guarded(work())

    async def SetOrganizationEnabled(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.SetOrganizationEnabledRequest, context: Any
    ) -> identity_pb2.SetOrganizationEnabledResponse:
        identity, tenant = await self._allow(
            request.target_tenant_id, step_up=True, permission=TENANT_PERMISSION
        )
        if tenant == identity.tenant_id and not request.enabled:
            raise InvalidRequestError("You cannot suspend your own tenant")

        async def work() -> identity_pb2.SetOrganizationEnabledResponse:
            current = await self._keycloak.organization(tenant)
            if current is None:
                raise NotFoundError("No such tenant")
            if bool(current.get("enabled")) != request.enabled:
                await self._keycloak.set_organization_enabled(current, request.enabled)
                async with self._engine.begin() as connection:
                    await audit(
                        connection,
                        tenant_id=tenant,
                        action=(
                            f"identity.organization.{'enabled' if request.enabled else 'disabled'}"
                        ),
                        actor_id=identity.user_id or "",
                        member_id=str(current["id"]),
                    )
            return identity_pb2.SetOrganizationEnabledResponse(enabled=request.enabled)

        return await self._guarded(work())
