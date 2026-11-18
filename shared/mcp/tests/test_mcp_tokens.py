"""A tool call's token: for this server alone, short-lived and scoped (MCP-001, MCP-004)."""

import time
from typing import Any

import pytest
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
from windops_mcp import TokenRefusedError, TokenSettings, TokenVerifier
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


@run_async
async def test_an_agents_own_identity_is_kept_when_the_token_names_it() -> None:
    """SEC-010: RFC 8693's actor claim, when M8.5 gives agents their own identity."""
    checker = verifier(Keycloak())
    caller = await checker.verify(token(act={"sub": "fault-investigator"}))
    await checker.close()
    assert (caller.agent, caller.user_id) == ("fault-investigator", USER)


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
