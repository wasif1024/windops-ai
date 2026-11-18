"""Keycloak's signing key, its published key set, and tokens shaped like the ones it exchanges."""

import asyncio
import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://keycloak.test/auth/realms/windops"
JWKS = "https://keycloak.test/auth/realms/windops/protocol/openid-connect/certs"
AUDIENCE = "windops-mcp-documentation"
TENANT = "north-sea"
USER = "user-1"
SEARCH = "mcp.documentation.search_documents"
READ = "mcp.documentation.read_passage"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_ID = "key-1"


def run_async[**P](test: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Run an async test, because the repository uses no async test plugin."""

    @functools.wraps(test)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


def published_keys() -> dict[str, Any]:
    public = RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    return {"keys": [{**public, "kid": KEY_ID, "use": "sig", "alg": "RS256"}]}


class Keycloak:
    """Serves the key set, and can be taken down."""

    def __init__(self) -> None:
        self.down = False
        self.fetches = 0

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.fetches += 1
            if self.down or str(request.url) != JWKS:
                return httpx.Response(503)
            return httpx.Response(200, json=published_keys())

        return httpx.MockTransport(handle)


def claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    found: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": USER,
        "tenant_id": TENANT,
        "iat": now,
        "exp": now + 120,
        "jti": "token-1",
        "azp": "windops-api",
        "scope": f"{AUDIENCE}-audience {SEARCH}",
    }
    found.update(changes)
    return {key: value for key, value in found.items() if value is not None}


def token(key: Any = KEY, key_id: str = KEY_ID, algorithm: str = "RS256", **changes: Any) -> str:
    return jwt.encode(claims(**changes), key, algorithm=algorithm, headers={"kid": key_id})
