"""The service's manage-realm role is held to a fixed list of calls."""

import asyncio

import pytest
from fake_keycloak import FakeKeycloak
from windops_identity.keycloak import ALLOWED, KeycloakAdmin, KeycloakSettings, NotAllowedError

# Changing this list widens what a compromised Identity Service could do in Keycloak. It must be
# reviewed as a security change, with the reason in the pull request.
EXPECTED = {
    ("GET", "organizations"),
    ("GET", "organizations/{org}/members"),
    ("GET", "organizations/{org}/members/{user}"),
    ("POST", "organizations/{org}/members"),
    ("GET", "users"),
    ("POST", "users"),
    ("GET", "users/{user}"),
    ("PUT", "users/{user}"),
    ("DELETE", "users/{user}"),
    ("PUT", "users/{user}/reset-password"),
    ("GET", "users/{user}/sessions"),
    ("POST", "users/{user}/logout"),
    ("GET", "users/{user}/credentials"),
}


def test_the_allowed_calls_are_exactly_the_reviewed_list() -> None:
    assert ALLOWED == EXPECTED


def test_nothing_reaches_realm_clients_roles_or_flows() -> None:
    for _, template in ALLOWED:
        assert template.split("/")[0] in {"organizations", "users"}
        assert "role-mappings" not in template
        assert "credentials/" not in template


@pytest.mark.parametrize(
    ("method", "template"),
    [
        ("PUT", ""),
        ("GET", "clients"),
        ("POST", "users/{user}/role-mappings/realm"),
        ("DELETE", "organizations"),
    ],
)
def test_any_other_call_is_refused_before_it_is_sent(method: str, template: str) -> None:
    keycloak = FakeKeycloak()
    admin = KeycloakAdmin(
        KeycloakSettings("http://keycloak", "windops", "client", "secret"),
        transport=keycloak.transport(),
    )

    async def scenario() -> None:
        try:
            with pytest.raises(NotAllowedError):
                await admin._call(method, template, ids={"user": "u"})
        finally:
            await admin.close()

    asyncio.run(scenario())
    assert keycloak.calls == []


def test_ids_are_escaped_so_they_cannot_change_the_path() -> None:
    keycloak = FakeKeycloak()
    admin = KeycloakAdmin(
        KeycloakSettings("http://keycloak", "windops", "client", "secret"),
        transport=keycloak.transport(),
    )

    async def scenario() -> None:
        try:
            await admin._call("GET", "users/{user}", ids={"user": "../clients"})
        finally:
            await admin.close()

    asyncio.run(scenario())
    assert keycloak.calls[-1] == ("GET", "/admin/realms/windops/users/..%2Fclients")
