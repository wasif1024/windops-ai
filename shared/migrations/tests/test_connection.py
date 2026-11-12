"""Signing in to PostgreSQL: a password locally, or Entra on Azure with none stored (ADR 0006)."""

from types import SimpleNamespace
from typing import Any

import pytest
from windops_migrations import EntraTokens, create_service_engine, database_auth
from windops_migrations.connection import (
    POSTGRES_SCOPE,
    RECYCLE_SECONDS,
    RENEW_SECONDS,
    public_roots,
)

DATABASE = {
    "WINDOPS_DATABASE_HOST": "windops-postgres.example",
    "WINDOPS_DATABASE_PORT": "5432",
    "WINDOPS_DATABASE_NAME": "windops_asset",
    "WINDOPS_DATABASE_USERNAME": "windops-asset",
    "WINDOPS_DATABASE_PASSWORD": "local-only",
    "WINDOPS_TLS_DIR": "/var/run/windops/tls",
}


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, "password"),
        ({"WINDOPS_ENVIRONMENT": "local"}, "password"),
        ({"WINDOPS_ENVIRONMENT": "dev"}, "entra"),
        ({"WINDOPS_ENVIRONMENT": "staging"}, "entra"),
        ({"WINDOPS_ENVIRONMENT": "test"}, "password"),
        ({"WINDOPS_ENVIRONMENT": "live", "WINDOPS_DATABASE_AUTH": "auto"}, "entra"),
        ({"WINDOPS_ENVIRONMENT": "live", "WINDOPS_DATABASE_AUTH": "password"}, "password"),
        ({"WINDOPS_ENVIRONMENT": "local", "WINDOPS_DATABASE_AUTH": "ENTRA"}, "entra"),
    ],
)
def test_the_environment_chooses_how_to_sign_in_unless_overridden(
    settings: dict[str, str], expected: str
) -> None:
    assert database_auth(settings) == expected


def test_an_unknown_way_to_sign_in_is_refused() -> None:
    with pytest.raises(ValueError, match="WINDOPS_DATABASE_AUTH"):
        database_auth({"WINDOPS_DATABASE_AUTH": "kerberos"})


class FakeCredential:
    def __init__(self, lifetime: float = 3600) -> None:
        self.calls = 0
        self.lifetime = lifetime
        self.now = 1_000_000.0

    def get_token(self, scope: str) -> Any:
        assert scope == POSTGRES_SCOPE
        self.calls += 1
        return SimpleNamespace(token=f"token-{self.calls}", expires_on=self.now + self.lifetime)


def test_a_token_is_kept_until_shortly_before_it_ends() -> None:
    credential = FakeCredential()
    tokens = EntraTokens(credential, clock=lambda: credential.now)
    assert tokens.current() == tokens.current() == "token-1"
    credential.now += 3600 - RENEW_SECONDS - 1
    assert tokens.current() == "token-1"
    credential.now += 2
    assert tokens.current() == "token-2"
    assert credential.calls == 2


def test_password_sign_in_is_as_before_and_checks_our_own_ca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in DATABASE.items():
        monkeypatch.setenv(name, value)
    engine = create_service_engine(auth="password")
    url = engine.url
    assert url.password == "local-only"
    assert url.query["sslrootcert"] == "/var/run/windops/tls/ca.crt"
    assert url.query["sslmode"] == "verify-full"


def test_entra_sign_in_stores_no_password_and_hands_each_connection_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in DATABASE.items():
        if name != "WINDOPS_DATABASE_PASSWORD":
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("WINDOPS_DATABASE_PASSWORD", raising=False)
    credential = FakeCredential()
    engine = create_service_engine(
        auth="entra", tokens=EntraTokens(credential, clock=lambda: credential.now)
    )
    url = engine.url
    assert url.password is None
    # Azure's certificate is public, so it is verified against certifi's public roots, never
    # skipped. Not libpq's "system", which psycopg's own OpenSSL cannot find on Azure.
    assert (url.query["sslmode"], url.query["sslrootcert"]) == ("verify-full", public_roots())
    assert engine.sync_engine.pool._recycle == RECYCLE_SECONDS
    cparams: dict[str, Any] = {}
    # do_connect is the dialect's event, so the listener lives there.
    for listener in engine.sync_engine.dialect.dispatch.do_connect:
        listener(engine.sync_engine.dialect, None, [], cparams)
    assert cparams["password"] == "token-1"


def test_entra_sign_in_needs_the_database_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINDOPS_DATABASE_HOST", raising=False)
    with pytest.raises(ValueError, match="WINDOPS_DATABASE_HOST"):
        create_service_engine(auth="entra", tokens=EntraTokens(FakeCredential()))


def test_the_public_roots_hold_the_authorities_azure_postgresql_chains_to() -> None:
    """Azure Database for PostgreSQL's certificates chain to these (Microsoft's TLS guidance)."""
    from pathlib import Path

    # certifi labels each certificate with its subject.
    subjects = [
        line
        for line in Path(public_roots()).read_text().splitlines()
        if line.startswith("# Subject:")
    ]
    for root in ("DigiCert Global Root G2", "Microsoft RSA Root Certificate Authority 2017"):
        assert any(f"CN={root}" in subject for subject in subjects), root
