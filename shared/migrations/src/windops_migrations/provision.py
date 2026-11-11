"""Give each service its own database and Entra login on Azure Database for PostgreSQL (ADR 0006).

    python -m windops_migrations provision-entra --host HOST --admin id-windops-postgres-admin \
        --role id-windops-audit=<object ID>=windops_audit ...

It runs in the cluster as a Job, because the server has private access only, signed in as the
server's one Entra administrator through Workload ID: no password exists. For each service it:

- creates a login for the service's managed identity, by object ID, so a renamed or re-created
  identity cannot take another's place;
- creates the service's database, and lets only that login connect to it and create in it.

The login gains nothing beyond its own database (ARC-002, DAT-005): no superuser, role or database
creation, replication or row-level security bypass. Locally, scripts/setup/provision_postgres.py
does the same with passwords. Safe to run repeatedly: it adds what is missing and removes nothing.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# Entra names Azure gives managed identities, and PostgreSQL's database names, both validated
# because SQL takes neither as a parameter. Identifiers are quoted as well.
_PRINCIPAL = re.compile(r"id-windops-[a-z]+(-[a-z]+)*")
_DATABASE = re.compile(r"windops_[a-z]+(_[a-z]+)*")
# Azure's own function: a login for an Entra principal, found by object ID, and not an admin.
CREATE_PRINCIPAL = (
    "SELECT * FROM pgaadauth_create_principal_with_oid(%s, %s, 'service', false, false)"
)
_OBJECT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Role:
    """A service's Entra login and its database."""

    principal: str
    object_id: str
    database: str

    @classmethod
    def parse(cls, text: str) -> "Role":
        """`id-windops-audit=<object ID>=windops_audit`."""
        parts = text.split("=")
        if len(parts) != 3:
            raise ValueError(f"A role is principal=object-id=database, not {text!r}")
        role = cls(*parts)
        if not _PRINCIPAL.fullmatch(role.principal):
            raise ValueError(f"Not a WindOps managed identity's name: {role.principal!r}")
        if not _OBJECT_ID.fullmatch(role.object_id):
            raise ValueError(f"Not an object ID: {role.object_id!r}")
        if not _DATABASE.fullmatch(role.database):
            raise ValueError(f"Not a WindOps database name: {role.database!r}")
        return role


def quoted(name: str) -> str:
    """An identifier, double-quoted. Names are validated first, so none holds a quote."""
    if '"' in name:
        raise ValueError(f"Unsupported name: {name!r}")
    return f'"{name}"'


def server_statements(
    role: Role, existing_roles: Iterable[str], existing_databases: Iterable[str]
) -> list[tuple[str, tuple[str, ...]]]:
    """(SQL, parameters) to run in the `postgres` database, one at a time, outside a transaction."""
    statements: list[tuple[str, tuple[str, ...]]] = []
    if role.principal not in set(existing_roles):
        statements.append((CREATE_PRINCIPAL, (role.principal, role.object_id)))
    database, login = quoted(role.database), quoted(role.principal)
    if role.database not in set(existing_databases):
        statements.append((f"CREATE DATABASE {database}", ()))
    statements += [
        (f"REVOKE ALL ON DATABASE {database} FROM PUBLIC", ()),
        (f"GRANT CONNECT, CREATE, TEMPORARY ON DATABASE {database} TO {login}", ()),
    ]
    return statements


def database_statements(role: Role) -> list[str]:
    """SQL to run in the service's own database: its login may create tables in `public`."""
    login = quoted(role.principal)
    return [
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        f"GRANT USAGE, CREATE ON SCHEMA public TO {login}",
    ]


def _connect(host: str, admin: str, database: str, token: str) -> Any:
    import psycopg

    return psycopg.connect(
        host=host,
        port=5432,
        dbname=database,
        user=admin,
        password=token,
        sslmode="verify-full",
        sslrootcert="system",
        autocommit=True,
        connect_timeout=15,
    )


def provision(host: str, admin: str, roles: Sequence[Role], tokens: Any = None) -> None:
    """Create every service's login and database, as the server's Entra administrator."""
    from windops_migrations.connection import EntraTokens

    tokens = tokens or EntraTokens()
    with _connect(host, admin, "postgres", tokens.current()) as connection:
        existing_roles = [row[0] for row in connection.execute("SELECT rolname FROM pg_roles")]
        existing_databases = [
            row[0] for row in connection.execute("SELECT datname FROM pg_database")
        ]
        for role in roles:
            for statement, values in server_statements(role, existing_roles, existing_databases):
                connection.execute(statement, values or None)
    for role in roles:
        with _connect(host, admin, role.database, tokens.current()) as connection:
            for statement in database_statements(role):
                connection.execute(statement)
        print(f"{role.principal} can use {role.database}")
