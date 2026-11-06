"""The browser's REST API, version 1. It translates to gRPC and holds no business rules (API-002).

Every route under /api/v1 except /public verifies the Keycloak token, applies the caller's rate
limit, and calls services as that user, with a short-lived signed internal identity.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

import grpc
from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from opentelemetry import metrics
from pydantic import BaseModel
from windops.authorization.v1 import authorization_pb2
from windops.tenant.v1 import tenant_pb2
from windops_grpc import VerifiedIdentity, sign_identity
from windops_resilience import BreakerOpenError

from windops_web_bff.admin import add_admin_routes
from windops_web_bff.branding import add_branding_routes
from windops_web_bff.errors import from_grpc, problem
from windops_web_bff.limits import LimitUnavailableError, RateLimiter, traffic_class
from windops_web_bff.platform import add_platform_routes
from windops_web_bff.security_metrics import SecurityCounters
from windops_web_bff.tokens import REASONS, Caller, TokenError, TokenVerifier, bearer

API_VERSION = "1.0.0"
PREFIX = "/api/v1"
SERVICE_SUBJECT = "svc-web-bff"
IDENTITY_SECONDS = 60
DEADLINE_SECONDS = 5.0
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class Me(BaseModel):
    user_id: str
    tenant_id: str
    email: str
    client: str
    # "mfa" once the user has used a second factor in this session.
    auth_level: str | None


class Branding(BaseModel):
    tenant_id: str
    display_name: str
    primary_color: str
    accent_color: str
    login_title: str
    login_message: str
    time_zone: str
    locale: str
    has_logo: bool


class ScopedRole(BaseModel):
    role: str
    # Both empty means the whole tenant.
    wind_farm_id: str
    turbine_id: str


class ScopedPermission(BaseModel):
    permission: str
    wind_farm_id: str
    turbine_id: str


class Permissions(BaseModel):
    """For showing the right screens. Every operation is still checked when it runs."""

    roles: list[ScopedRole]
    permissions: list[ScopedPermission]


class Tenant(BaseModel):
    tenant_id: str
    name: str
    status: str


@dataclass
class Dependencies:
    verifier: TokenVerifier
    limiter: RateLimiter
    identity_secret: str
    # Calls one Tenant Service method with an identity token, through its breaker.
    tenant: Callable[..., Awaitable[Any]]
    # Calls one Authorization Service method the same way. These three also take `retry`, which is
    # False for a change, so only the browser resends it, with the same idempotency key.
    authorization: Callable[..., Awaitable[Any]]
    identity: Callable[..., Awaitable[Any]]
    audit: Callable[..., Awaitable[Any]]
    # For the Security dashboard. main.py starts them as the service starts.
    counters: SecurityCounters | None = None
    # Only for tests, when there are no counters: the global provider when unset.
    meter_provider: metrics.MeterProvider | None = None


class ApiError(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


def identity_for(caller: Caller, signing_key: str) -> str:
    """Sign the internal identity services trust, copying how the user signed in (SEC-003)."""
    return sign_identity(
        VerifiedIdentity(
            subject=caller.user_id,
            tenant_id=caller.tenant_id,
            user_id=caller.user_id,
            auth_level=caller.auth_level,
            auth_time=caller.auth_time,
        ),
        signing_key,
        ttl_seconds=IDENTITY_SECONDS,
    )


def service_identity_for(tenant_id: str, signing_key: str) -> str:
    """Act for a tenant before anyone signs in, which allows only reading what is public."""
    return sign_identity(
        VerifiedIdentity(subject=SERVICE_SUBJECT, tenant_id=tenant_id),
        signing_key,
        ttl_seconds=30,
    )


def _branding(message: Any) -> Branding:
    return Branding(
        tenant_id=message.tenant_id,
        display_name=message.display_name,
        primary_color=message.primary_color,
        accent_color=message.accent_color,
        login_title=message.login_title,
        login_message=message.login_message,
        time_zone=message.time_zone,
        locale=message.locale,
        has_logo=message.has_logo,
    )


def create_router(deps: Dependencies) -> APIRouter:
    router = APIRouter(prefix=PREFIX)
    counters = deps.counters or SecurityCounters(deps.meter_provider)

    async def caller(request: Request, response: Response) -> Caller:
        try:
            verified = await deps.verifier.verify(bearer(request.headers.get("authorization")))
        except TokenError as error:
            code, title = REASONS[error.reason]
            counters.token_refused(error.reason)
            raise ApiError(problem(401, title, code, **{"WWW-Authenticate": "Bearer"})) from error
        try:
            limit = await deps.limiter.check(verified.tenant_id, verified.user_id, verified.client)
        except LimitUnavailableError as error:
            raise ApiError(problem(503, "Temporarily unavailable", "unavailable")) from error
        headers = {
            "RateLimit-Limit": str(limit.limit),
            "RateLimit-Remaining": str(limit.remaining),
            "RateLimit-Reset": str(limit.reset_seconds),
        }
        if not limit.allowed:
            counters.rate_limited(traffic_class(verified.client))
            raise ApiError(
                problem(
                    429, "Too many requests", **headers, **{"Retry-After": str(limit.reset_seconds)}
                )
            )
        response.headers.update(headers)
        return verified

    async def answer(attempt: Awaitable[Any]) -> Any:
        try:
            return await attempt
        except grpc.aio.AioRpcError as error:
            raise ApiError(from_grpc(error)) from error
        except (BreakerOpenError, TimeoutError) as error:
            raise ApiError(problem(503, "Temporarily unavailable", "unavailable")) from error

    async def call(method: str, request: Any, token: str, service: str = "tenant") -> Any:
        target = deps.authorization if service == "authorization" else deps.tenant
        return await answer(target(method, request, token))

    services = {
        "tenant": deps.tenant,
        "identity": deps.identity,
        "authorization": deps.authorization,
        "audit": deps.audit,
    }

    async def call_as(
        service: str, method: str, request: Any, who: Caller, *, retry: bool = True
    ) -> Any:
        """Call as the signed-in user, so the service decides for them (SEC-003)."""
        token = identity_for(who, deps.identity_secret)
        return await answer(services[service](method, request, token, retry=retry))

    @router.get("/me", response_model=Me, summary="Who is signed in")
    async def me(who: Annotated[Caller, Depends(caller)]) -> Me:
        return Me(
            user_id=who.user_id,
            tenant_id=who.tenant_id,
            email=who.email,
            client=who.client,
            auth_level=who.auth_level,
        )

    @router.get(
        "/me/permissions",
        response_model=Permissions,
        summary="What the signed-in user may do, so a screen shows only that",
    )
    async def my_permissions(who: Annotated[Caller, Depends(caller)]) -> Permissions:
        answer = await call(
            "ListMyPermissions",
            authorization_pb2.ListMyPermissionsRequest(),
            identity_for(who, deps.identity_secret),
            service="authorization",
        )
        return Permissions(
            roles=[
                ScopedRole(
                    role=item.role,
                    wind_farm_id=item.scope.wind_farm_id,
                    turbine_id=item.scope.turbine_id,
                )
                for item in answer.roles
            ],
            permissions=[
                ScopedPermission(
                    permission=item.permission,
                    wind_farm_id=item.scope.wind_farm_id,
                    turbine_id=item.scope.turbine_id,
                )
                for item in answer.permissions
            ],
        )

    @router.get("/tenant", response_model=Tenant, summary="The signed-in user's tenant")
    async def tenant(who: Annotated[Caller, Depends(caller)]) -> Tenant:
        answer = await call(
            "GetTenant", tenant_pb2.GetTenantRequest(), identity_for(who, deps.identity_secret)
        )
        return Tenant(
            tenant_id=answer.tenant.tenant_id, name=answer.tenant.name, status=answer.tenant.status
        )

    @router.get("/branding", response_model=Branding, summary="The signed-in user's branding")
    async def branding(who: Annotated[Caller, Depends(caller)]) -> Branding:
        answer = await call(
            "GetBranding", tenant_pb2.GetBrandingRequest(), identity_for(who, deps.identity_secret)
        )
        return _branding(answer.branding)

    @router.get(
        "/public/branding/{tenant_id}",
        response_model=Branding,
        summary="A tenant's branding, for its sign-in page, before anyone signs in",
    )
    async def public_branding(tenant_id: str) -> Branding:
        if not _TENANT.match(tenant_id):
            raise ApiError(problem(404, "Not found", "not_found"))
        answer = await call(
            "GetBranding",
            tenant_pb2.GetBrandingRequest(),
            service_identity_for(tenant_id, deps.identity_secret),
        )
        # The custom domain's proof token is not public, so only the display fields go out.
        return _branding(answer.branding)

    @router.get(
        "/public/branding/{tenant_id}/logo",
        summary="A tenant's logo, for its sign-in page",
        response_class=Response,
        responses={200: {"content": {"image/png": {}, "image/jpeg": {}}}},
    )
    async def public_logo(tenant_id: str) -> Response:
        if not _TENANT.match(tenant_id):
            raise ApiError(problem(404, "Not found", "not_found"))
        answer = await call(
            "GetLogo",
            tenant_pb2.GetLogoRequest(),
            service_identity_for(tenant_id, deps.identity_secret),
        )
        return Response(
            answer.image,
            media_type=answer.content_type,
            headers={
                "Cache-Control": "public, max-age=300",
                # Never sniffed into something that runs.
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    add_admin_routes(router, caller, call_as)
    add_branding_routes(router, caller, call_as, ApiError)
    add_platform_routes(router, caller, call_as)

    @router.get("/openapi.json", include_in_schema=False)
    async def openapi() -> JSONResponse:
        return JSONResponse(openapi_document(router))

    return router


def openapi_document(router: APIRouter) -> dict[str, Any]:
    """The published contract, versioned with the path (API-002)."""
    document: dict[str, Any] = get_openapi(
        title="WindOps Web BFF",
        version=API_VERSION,
        description="The browser's REST API. Send `Authorization: Bearer <Keycloak access token>`.",
        routes=router.routes,
    )
    return document


async def _handle(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ApiError):
        return problem(500, "Internal error")
    return error.response


async def _invalid(request: Request, error: Exception) -> JSONResponse:
    """A problem in our own words, which never repeats what was sent."""
    return problem(422, "The request is not valid", "invalid_request")


# For run_service, which adds them to the HTTP app.
EXCEPTION_HANDLERS: dict[type[Exception], Callable[[Any, Exception], Awaitable[Any]]] = {
    ApiError: _handle,
    RequestValidationError: _invalid,
}
