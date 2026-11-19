"""A tool call's token: for this server alone, short-lived and scoped (MCP-001, MCP-004)."""

import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from mcp_support import (
    AUDIENCE,
    ISSUER,
    JWKS,
    OTHER_KEY,
    SEARCH,
    TENANT,
    USER,
    Keycloak,
    run_async,
    token,
)
from windops_mcp import AgentIssuer, TokenRefusedError, TokenSettings, TokenVerifier
from windops_mcp.tokens import bearer


def verifier(keycloak: Keycloak, **options: Any) -> TokenVerifier:
    return TokenVerifier(
        TokenSettings(ISSUER, AUDIENCE, JWKS), transport=keycloak.transport(), **options
    )


async def reason_for(raw: str, keycloak: Keycloak | None = None) -> str:
    checker = verifier(keycloak or Keycloak())
    try:
        await checker.verify(raw)
    except TokenRefusedError as error:
        return error.reason
    finally:
        await checker.close()
    return "accepted"


@run_async
async def test_an_exchanged_token_names_its_user_tenant_and_tools() -> None:
    checker = verifier(Keycloak())
    caller = await checker.verify(token())
    await checker.close()
    assert (caller.user_id, caller.tenant_id) == (USER, TENANT)
    assert SEARCH in caller.scopes
    assert caller.token_id == "token-1"
    assert caller.agent == ""


AGENT_ISSUER = "windops-identity-agents"
AGENT_KEY = ec.generate_private_key(ec.SECP256R1())
AGENT_KEY_ID = "agent-key-1"


def agent_token(key: Any = AGENT_KEY, **changes: Any) -> str:
    issued = int(time.time())
    claims: dict[str, Any] = {
        "iss": AGENT_ISSUER, "sub": USER, "aud": AUDIENCE, "tenant_id": TENANT,
        "scope": SEARCH, "act": {"sub": "agent:reliability-engineer"},
        "iat": issued, "exp": issued + 120, "jti": "agent-token-1",
    }  # fmt: skip
    claims.update(changes)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": AGENT_KEY_ID})


def agent_verifier() -> TokenVerifier:
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(AGENT_KEY.public_key()))

    async def keys() -> list[dict[str, Any]]:
        return [{**public, "kid": AGENT_KEY_ID, "use": "sig", "alg": "ES256"}]

    return verifier(Keycloak(), agents=AgentIssuer(AGENT_ISSUER, keys))


@run_async
async def test_an_agent_token_names_the_agent_and_the_principal_behind_it() -> None:
    """SEC-010, ADR 0019: signed by the Identity Service, naming the agent in RFC 8693's act."""
    checker = agent_verifier()
    caller = await checker.verify(agent_token())
    service = await checker.verify(agent_token(sub="service:fault-investigator"))
    await checker.close()
    assert (caller.agent, caller.user_id, caller.is_service) == (
        "agent:reliability-engineer", USER, False,
    )  # fmt: skip
    assert service.is_service and service.agent == "agent:reliability-engineer"


@run_async
async def test_only_the_identity_service_names_agents_and_its_tokens_must_name_one() -> None:
    checker = agent_verifier()
    refused = []
    for raw in (
        # Keycloak's own token naming an agent: only the Identity Service names agents.
        token(act={"sub": "agent:supervisor"}),
        # The agent issuer's token with no agent, or not an agent, in act.
        agent_token(act=None),
        agent_token(act={"sub": "fault-investigator"}),
        # Signed by another key, or with Keycloak's algorithm, under the agent issuer's name.
        agent_token(key=ec.generate_private_key(ec.SECP256R1())),
        token(iss=AGENT_ISSUER),
        # An issuer nobody trusts.
        agent_token(iss="someone-else"),
    ):
        try:
            await checker.verify(raw)
            refused.append("accepted")
        except TokenRefusedError as error:
            refused.append(error.reason)
    await checker.close()
    assert refused == ["invalid"] * 6
    # Without the agent issuer configured, an agent token is not accepted at all.
    assert await reason_for(agent_token()) == "invalid"


now = int(time.time())


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        # A user's own login is for the platform's API, not for this server.
        ({"aud": "windops-api"}, "wrong_audience"),
        ({"aud": [AUDIENCE, "windops-api"]}, "wrong_audience"),
        ({"aud": "windops-mcp-tavily"}, "wrong_audience"),
        ({"aud": []}, "wrong_audience"),
        ({"iat": now, "exp": now + 3600}, "too_long_lived"),
        ({"iat": now - 400, "exp": now - 300}, "invalid"),
        ({"iat": now + 600, "exp": now + 700}, "invalid"),
        ({"nbf": now + 600}, "invalid"),
        ({"tenant_id": None}, "no_tenant"),
        ({"tenant_id": ""}, "no_tenant"),
        ({"tenant_id": ["north-sea", "iberia"]}, "no_tenant"),
        ({"iss": "https://elsewhere.test/realms/windops"}, "invalid"),
        ({"sub": None}, "invalid"),
        ({"iat": None}, "invalid"),
        ({"exp": None}, "invalid"),
    ],
)
@run_async
async def test_a_token_this_server_should_not_accept_is_refused(
    changes: dict[str, Any], reason: str
) -> None:
    assert await reason_for(token(**changes)) == reason


@run_async
async def test_a_token_signed_with_another_key_is_refused() -> None:
    assert await reason_for(token(key=OTHER_KEY)) == "invalid"
    assert await reason_for(token(key=OTHER_KEY, key_id="unknown")) == "invalid"


@run_async
async def test_a_token_signed_with_a_shared_secret_is_refused() -> None:
    forged = token(key="a-shared-secret-of-at-least-32-bytes!", algorithm="HS256")
    assert await reason_for(forged) == "invalid"


@run_async
async def test_garbage_is_refused() -> None:
    assert await reason_for("not.a.token") == "invalid"


@run_async
async def test_without_keycloaks_keys_nothing_is_accepted() -> None:
    keycloak = Keycloak()
    keycloak.down = True
    assert await reason_for(token(), keycloak) == "keys_unavailable"


@run_async
async def test_keys_are_fetched_once_and_again_only_after_a_while() -> None:
    keycloak = Keycloak()
    clock = [1000.0]
    checker = verifier(keycloak, clock=lambda: clock[0])
    await checker.verify(token())
    await checker.verify(token())
    for _ in range(3):
        with pytest.raises(TokenRefusedError):
            await checker.verify(token(key=OTHER_KEY, key_id="unknown"))
    assert keycloak.fetches == 1
    clock[0] += 61
    with pytest.raises(TokenRefusedError):
        await checker.verify(token(key=OTHER_KEY, key_id="unknown"))
    assert keycloak.fetches == 2
    await checker.close()


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer   ", "Basic abc", "token"])
def test_a_request_without_a_bearer_token_is_refused(header: str | None) -> None:
    with pytest.raises(TokenRefusedError) as raised:
        bearer(header)
    assert raised.value.reason == "missing"


def test_a_refusal_says_only_its_own_safe_message() -> None:
    assert TokenRefusedError("something internal").reason == "invalid"
    assert "internal" not in TokenRefusedError("something internal").message


def test_an_agent_is_forwarded_as_the_actor_and_fault_investigator_as_a_service() -> None:
    """SEC-009: services see the agent, and hold it to reading and drafting."""
    from windops_auth import current_identity, current_identity_token, verify_identity
    from windops_mcp import ToolCaller, acting_for_caller, caller_scope

    secret = "s" * 32
    expires = int(time.time()) + 100
    for subject, person in ((USER, True), ("service:fault-investigator", False)):
        caller = ToolCaller(
            user_id=subject, tenant_id=TENANT, scopes=frozenset({SEARCH}), expires_at=expires,
            agent="agent:supervisor", auth_level="mfa" if person else None,
        )  # fmt: skip
        with caller_scope(caller), acting_for_caller(secret):
            forwarded = verify_identity(current_identity_token(), secret)
            assert forwarded == current_identity()
        assert forwarded.actor == "agent:supervisor"
        assert forwarded.subject == subject
        assert (forwarded.user_id == subject) is person
