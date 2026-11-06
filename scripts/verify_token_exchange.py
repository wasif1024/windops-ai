#!/usr/bin/env python3
"""Prove token exchange on the real cluster (SEC-008), with a real login and real Keycloak.

Run `make identity-seed`, `make services` and `make gateway-routes` first. Needs local port 9443.
"""

import base64
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
import jwt
from gateway_routes import port_forward
from identity_seed import (
    EXCHANGE_LIFETIME_SECONDS,
    PUBLIC_URL,
    REALM,
    demo_email,
    keycloak_public,
    read_demo_passwords,
)
from tls_client import blocking_channel, client_bundle
from verify_identity import Realm
from windops.identity.v1 import identity_pb2 as pb
from windops.identity.v1 import identity_pb2_grpc
from windops_grpc import VerifiedIdentity, sign_identity

SYSTEM = "windops-system"
TENANT = "north-sea"
AGENT = "windops-agent-runtime"
HOST = "windops-identity.windops-system.svc.cluster.local"
API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def signing_key() -> str:
    encoded = subprocess.run(
        [
            "kubectl", "--context", "windops", "get", "secret", "windops-internal-identity",
            f"--namespace={SYSTEM}", "--output=jsonpath={.data.signing-key}",
        ],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return base64.b64decode(encoded).decode()


def verified(token: str, audience: str) -> dict[str, Any]:
    """Check the token as its receiver must: Keycloak's signature, the issuer and the audience."""
    keys = httpx.get(
        f"{PUBLIC_URL}/realms/{REALM}/protocol/openid-connect/certs", timeout=10
    ).json()
    kid = jwt.get_unverified_header(token)["kid"]
    key = next(item for item in keys["keys"] if item["kid"] == kid)
    claims: dict[str, Any] = jwt.decode(
        token,
        jwt.PyJWK(key).key,
        algorithms=["RS256"],
        audience=audience,
        issuer=f"{PUBLIC_URL}/realms/{REALM}",
    )
    return claims


class Caller:
    def __init__(self, stub: Any, key: str) -> None:
        self.stub, self.key = stub, key

    def exchange(self, user: str, token: str, audience: str = AGENT) -> Any:
        identity = VerifiedIdentity(subject=user, tenant_id=TENANT, user_id=user)
        metadata = (("x-windops-identity", sign_identity(identity, self.key, ttl_seconds=60)),)
        return self.stub.ExchangeToken(
            pb.ExchangeTokenRequest(subject_token=token, audience=audience),
            metadata=metadata,
            timeout=15,
        )

    def code(self, user: str, token: str, audience: str = AGENT) -> grpc.StatusCode:
        try:
            self.exchange(user, token, audience)
        except grpc.RpcError as error:
            status: grpc.StatusCode = error.code()
            return status
        return grpc.StatusCode.OK


def run(realm: Realm, caller: Caller) -> None:
    tokens = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if tokens is None:
        raise RuntimeError("The demo operator could not sign in")
    user = verified(tokens.access, "windops-api")
    operator = str(user["sub"])
    started = time.time()
    response = caller.exchange(operator, tokens.access)
    exchanged = verified(response.access_token, AGENT)
    check(
        exchanged["sub"] == operator and exchanged["aud"] == AGENT,
        "A user's own token becomes one for the agent runtime alone, for the same user",
    )
    check(exchanged.get("tenant_id") == TENANT, "It keeps the user's tenant")
    lifetime = exchanged["exp"] - exchanged["iat"]
    check(
        lifetime <= EXCHANGE_LIFETIME_SECONDS and exchanged["exp"] <= started + 180,
        f"It lasts {lifetime} seconds, against the user's 5 minutes",
    )
    refused = httpx.get(f"{API}/me", headers={"Authorization": f"Bearer {response.access_token}"})
    # The gateway answers a wrong audience with 403, and Web BFF would answer with a problem.
    check(
        refused.status_code in (401, 403)
        and refused.headers.get("content-type") != "application/problem+json",
        "The gateway refuses it on the user's own API, because it is only for the agent runtime",
    )
    admin = f"someone-other-than-{operator}"
    check(
        caller.code(admin, tokens.access) is grpc.StatusCode.PERMISSION_DENIED,
        "Another user, or a service holding this token, cannot exchange it",
    )
    check(
        caller.code(operator, tokens.access, "windops-admin") is grpc.StatusCode.INVALID_ARGUMENT,
        "A token cannot be exchanged for a service that is not allowed",
    )
    check(
        caller.code(operator, "not.a.token") is grpc.StatusCode.PERMISSION_DENIED,
        "A forged token cannot be exchanged",
    )


def main() -> int:
    try:
        bundle, key = client_bundle(), signing_key()
        with (
            keycloak_public() as public,
            port_forward("deployment/windops-identity", SYSTEM, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            run(Realm(public), Caller(identity_pb2_grpc.IdentityServiceStub(channel), key))
    except (RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, jwt.PyJWTError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Token exchange check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
