"""Web BFF's API through real HTTP requests, with real RSA-signed tokens and fake dependencies."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from windops.tenant.v1 import tenant_pb2
from windops_auth import LEVEL_MFA, STEP_UP_MESSAGE
from windops_grpc import verify_identity
from windops_web_bff.api import EXCEPTION_HANDLERS, PREFIX, Dependencies, create_router
from windops_web_bff.limits import LIMITS, RateLimiter
from windops_web_bff.tokens import TokenSettings, TokenVerifier

ISSUER = "https://localhost:9443/auth/realms/windops"
AUDIENCE = "windops-api"
JWKS = "https://keycloak.test/certs"
SECRET = "x" * 32
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks() -> dict[str, Any]:
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**public, "kid": "k1", "alg": "RS256", "use": "sig"}]}


def token(key: Any = KEY, kid: str = "k1", algorithm: str = "RS256", **claims: Any) -> str:
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "u-1",
        "azp": "windops-web",
        "tenant_id": "north-sea",
        "email": "operator@north-sea.example",
        "iat": now,
        "exp": now + 300,
        "acr": LEVEL_MFA,
        "auth_time": now,
        **claims,
    }
    body = {name: value for name, value in body.items() if value is not None}
    return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})


class FakeRedis:
    def __init__(self, *, down: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.down = down

    async def incrby(self, name: str, amount: int) -> int:
        if self.down:
            raise ConnectionError("Redis is down")
        self.counts[name] = self.counts.get(name, 0) + amount
        return self.counts[name]

    async def expire(self, name: str, time: int) -> bool:
        return True


@dataclass
class FakeTenant:
    """Stands in for the Tenant Service, recording the identity each call carried."""

    calls: list[tuple[str, str]] = field(default_factory=list)
    error: grpc.aio.AioRpcError | None = None

    async def __call__(self, method: str, request: Any, identity: str) -> Any:
        self.calls.append((method, identity))
        if self.error is not None:
            raise self.error
        claims = verify_identity(identity, SECRET)
        if method == "GetTenant":
            return tenant_pb2.GetTenantResponse(
                tenant=tenant_pb2.Tenant(
                    tenant_id=claims.tenant_id, name="North Sea", status="active"
                )
            )
        if method == "GetLogo":
            return tenant_pb2.GetLogoResponse(image=b"\x89PNG\r\n\x1a\n", content_type="image/png")
        return tenant_pb2.GetBrandingResponse(
            branding=tenant_pb2.Branding(tenant_id=claims.tenant_id, display_name="North Sea"),
            custom_domain=tenant_pb2.CustomDomain(domain="x.example", verification_token="secret"),
        )


def rpc_error(code: grpc.StatusCode, details: str) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata(), details)


@dataclass
class World:
    client: httpx.AsyncClient
    tenant: FakeTenant
    redis: FakeRedis


def keycloak() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json=jwks()))


def with_bff(
    scenario: Callable[[World], Awaitable[None]], *, redis: FakeRedis | None = None
) -> None:
    async def run() -> None:
        verifier = TokenVerifier(TokenSettings(ISSUER, AUDIENCE, JWKS), transport=keycloak())
        tenant, counter = FakeTenant(), redis or FakeRedis()
        app = FastAPI()
        app.include_router(
            create_router(
                Dependencies(
                    verifier=verifier,
                    limiter=RateLimiter(counter),
                    identity_secret=SECRET,
                    tenant=tenant,
                )
            )
        )
        for kind, handler in EXCEPTION_HANDLERS.items():
            app.add_exception_handler(kind, handler)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bff") as client:
            await scenario(World(client, tenant, counter))
        await verifier.close()

    asyncio.run(run())


def auth(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_a_verified_user_is_told_who_they_are() -> None:
    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/me", headers=auth(token()))
        assert response.status_code == 200
        assert response.json()["tenant_id"] == "north-sea"
        assert response.headers["RateLimit-Limit"] == str(LIMITS["standard"])

    with_bff(scenario)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not-a-token",
        token(iss="https://evil.example/realms/windops"),
        token(aud="someone-else"),
        token(exp=int(time.time()) - 60, iat=int(time.time()) - 400),
        token(key=OTHER_KEY),
        token(kid="unknown"),
        # A token with no tenant: a suspended tenant's user, or one in two tenants (SEC-005).
        token(tenant_id=None),
        token(tenant_id=["north-sea", "atlas-wind"]),
        token(sub=None),
    ],
)
def test_a_bad_token_is_refused(bad: str | None) -> None:
    async def scenario(world: World) -> None:
        headers = auth(bad) if bad else {}
        response = await world.client.get(f"{PREFIX}/tenant", headers=headers)
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
        assert world.tenant.calls == []

    with_bff(scenario)


def test_a_token_signed_with_hs256_and_the_public_key_is_refused() -> None:
    """The classic algorithm confusion: RS256 is the only one accepted."""
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "u-1", "tenant_id": "north-sea",
         "iat": int(time.time()), "exp": int(time.time()) + 300},
        "x" * 64,
        algorithm="HS256",
        headers={"kid": "k1"},
    )  # fmt: skip

    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/me", headers=auth(forged))
        assert response.status_code == 401

    with_bff(scenario)


def test_services_are_called_as_the_user_with_how_they_signed_in() -> None:
    """SEC-003: the second factor's time travels with the call, so services can require step-up."""

    async def scenario(world: World) -> None:
        signed_at = int(time.time()) - 30
        await world.client.get(f"{PREFIX}/tenant", headers=auth(token(auth_time=signed_at)))
        identity = verify_identity(world.tenant.calls[0][1], SECRET)
        assert (identity.user_id, identity.tenant_id) == ("u-1", "north-sea")
        assert (identity.auth_level, identity.auth_time) == (LEVEL_MFA, signed_at)

    with_bff(scenario)


def test_a_users_limit_is_enforced_per_minute() -> None:
    async def scenario(world: World) -> None:
        bearer = auth(token())
        for _ in range(LIMITS["standard"]):
            assert (await world.client.get(f"{PREFIX}/me", headers=bearer)).status_code == 200
        refused = await world.client.get(f"{PREFIX}/me", headers=bearer)
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 0

    with_bff(scenario)


def test_an_administrator_gets_the_higher_limit() -> None:
    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/me", headers=auth(token(azp="windops-admin")))
        assert response.headers["RateLimit-Limit"] == str(LIMITS["administrator"])

    with_bff(scenario)


def test_limits_fail_closed_when_redis_is_down() -> None:
    """SRS 7.7."""

    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/me", headers=auth(token()))
        assert response.status_code == 503

    with_bff(scenario, redis=FakeRedis(down=True))


def test_a_step_up_refusal_asks_for_a_second_factor() -> None:
    """RFC 9470: the browser can tell this from any other refusal, and sign in again."""

    async def scenario(world: World) -> None:
        world.tenant.error = rpc_error(grpc.StatusCode.PERMISSION_DENIED, STEP_UP_MESSAGE)
        response = await world.client.get(f"{PREFIX}/tenant", headers=auth(token()))
        assert response.status_code == 401
        challenge = response.headers["WWW-Authenticate"]
        assert 'error="insufficient_user_authentication"' in challenge
        assert 'acr_values="mfa"' in challenge

    with_bff(scenario)


@pytest.mark.parametrize(
    ("code", "status", "name"),
    [
        (grpc.StatusCode.PERMISSION_DENIED, 403, "forbidden"),
        (grpc.StatusCode.NOT_FOUND, 404, "not_found"),
        (grpc.StatusCode.INVALID_ARGUMENT, 400, "invalid_request"),
        (grpc.StatusCode.FAILED_PRECONDITION, 409, "conflict"),
        (grpc.StatusCode.UNAVAILABLE, 503, "unavailable"),
        (grpc.StatusCode.INTERNAL, 502, "bad_gateway"),
    ],
)
def test_service_answers_become_http_with_none_of_the_services_words(
    code: grpc.StatusCode, status: int, name: str
) -> None:
    """The BFF is the edge, so a service's own message never reaches the browser."""

    async def scenario(world: World) -> None:
        world.tenant.error = rpc_error(code, "the database at 10.0.0.5 said no")
        response = await world.client.get(f"{PREFIX}/tenant", headers=auth(token()))
        assert response.status_code == status
        assert response.json()["code"] == name
        assert "10.0.0.5" not in response.text and "database" not in response.text

    with_bff(scenario)


def test_a_refused_token_says_why_in_our_own_words() -> None:
    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/me", headers=auth(token(tenant_id=None)))
        assert response.json()["code"] == "no_tenant"

    with_bff(scenario)


def test_public_branding_needs_no_sign_in_and_leaves_out_the_domain_token() -> None:
    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/public/branding/north-sea")
        assert response.status_code == 200
        assert "secret" not in response.text and "verification" not in response.text
        identity = verify_identity(world.tenant.calls[0][1], SECRET)
        # A service identity, with no user, so services allow only what any reader may see.
        assert identity.user_id is None and identity.tenant_id == "north-sea"

    with_bff(scenario)


@pytest.mark.parametrize("tenant", ["North-Sea", "a", "..%2Fadmin", "x" * 70])
def test_a_bad_tenant_in_a_public_path_is_not_found(tenant: str) -> None:
    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/public/branding/{tenant}")
        assert response.status_code == 404
        assert world.tenant.calls == []

    with_bff(scenario)


def test_a_logo_is_served_so_a_browser_cannot_run_it() -> None:
    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/public/branding/north-sea/logo")
        assert response.headers["content-type"] == "image/png"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Security-Policy"] == "default-src 'none'"

    with_bff(scenario)


def test_the_versioned_openapi_document_is_published() -> None:
    """API-002."""

    async def scenario(world: World) -> None:
        document = (await world.client.get(f"{PREFIX}/openapi.json")).json()
        assert document["info"]["version"] == "1.0.0"
        assert {f"{PREFIX}/me", f"{PREFIX}/tenant", f"{PREFIX}/branding"} <= set(document["paths"])
        assert f"{PREFIX}/openapi.json" not in document["paths"]

    with_bff(scenario)
