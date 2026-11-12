"""Each service's Entra login and database on Azure Database for PostgreSQL (ADR 0006)."""

from typing import Any

import pytest
from windops_migrations import provision
from windops_migrations.provision import Role, database_statements, server_statements

OBJECT_ID = "00000000-1111-2222-3333-444444444444"
AUDIT = Role("id-windops-audit", OBJECT_ID, "windops_audit")


def test_a_role_is_a_managed_identity_its_object_id_and_its_database() -> None:
    assert Role.parse(f"id-windops-audit={OBJECT_ID}=windops_audit") == AUDIT
    for wrong in (
        f"id-windops-audit={OBJECT_ID}",
        f'id-windops-audit"; DROP DATABASE x; --={OBJECT_ID}=windops_audit',
        f"postgres={OBJECT_ID}=windops_audit",  # not a WindOps identity
        "id-windops-audit=not-an-id=windops_audit",
        f"id-windops-audit={OBJECT_ID}=postgres",  # not a WindOps database
        f"id-windops-audit={OBJECT_ID}=windops_audit; DROP",
    ):
        with pytest.raises(ValueError):
            Role.parse(wrong)


def test_a_new_service_gets_a_login_by_object_id_and_its_own_database() -> None:
    statements = server_statements(AUDIT, existing_roles=[], existing_databases=[])
    sql = [statement for statement, _ in statements]
    assert statements[0] == (
        "SELECT * FROM pgaadauth_create_principal_with_oid(%s, %s, 'service', false, false)",
        ("id-windops-audit", OBJECT_ID),
    )
    assert sql[1:] == [
        'CREATE DATABASE "windops_audit"',
        'REVOKE ALL ON DATABASE "windops_audit" FROM PUBLIC',
        'GRANT CONNECT, CREATE, TEMPORARY ON DATABASE "windops_audit" TO "id-windops-audit"',
    ]
    # Not an administrator, and nothing beyond its own database (ARC-002, DAT-005).
    assert not any("azure_pg_admin" in text or "SUPERUSER" in text for text in sql)


def test_running_again_creates_nothing_twice_and_restores_the_grants() -> None:
    sql = [s for s, _ in server_statements(AUDIT, ["id-windops-audit"], ["windops_audit"])]
    assert not any("pgaadauth" in text or "CREATE DATABASE" in text for text in sql)
    assert any(text.startswith("REVOKE ALL ON DATABASE") for text in sql)


def test_only_the_service_may_create_tables_in_its_database() -> None:
    assert database_statements(AUDIT) == [
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        'GRANT USAGE, CREATE ON SCHEMA public TO "id-windops-audit"',
    ]


class FakeConnection:
    def __init__(self, database: str, log: list[tuple[str, str, Any]]) -> None:
        self.database, self.log = database, log

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, statement: str, values: Any = None) -> list[tuple[str]]:
        self.log.append((self.database, statement, values))
        return [("id-windops-tenant",)] if "pg_roles" in statement else [("postgres",)]


class FakeTokens:
    def current(self) -> str:
        return "token"


def test_provisioning_signs_in_as_the_entra_administrator_with_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[str, str, Any]] = []
    signed_in: list[tuple[str, str, str]] = []

    def connect(host: str, admin: str, database: str, token: str) -> FakeConnection:
        signed_in.append((admin, database, token))
        return FakeConnection(database, log)

    monkeypatch.setattr(provision, "_connect", connect)
    tenant = Role("id-windops-tenant", OBJECT_ID, "windops_tenant")
    provision.provision("server", "id-windops-postgres-admin", [AUDIT, tenant], FakeTokens())
    assert signed_in == [
        ("id-windops-postgres-admin", "postgres", "token"),
        ("id-windops-postgres-admin", "windops_audit", "token"),
        ("id-windops-postgres-admin", "windops_tenant", "token"),
    ]
    created = [values for _, statement, values in log if "pgaadauth" in statement]
    assert created == [("id-windops-audit", OBJECT_ID)]  # the tenant's login already exists


def test_provisioning_verifies_the_server_against_the_public_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg
    from windops_migrations.connection import public_roots

    seen: dict[str, Any] = {}
    monkeypatch.setattr(psycopg, "connect", lambda **options: seen.update(options))
    provision._connect("server", "id-windops-postgres-admin", "postgres", "token")
    assert seen["sslmode"] == "verify-full"
    assert seen["sslrootcert"] == public_roots()
