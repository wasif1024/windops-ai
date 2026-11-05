"""PostgreSQL row-level security for tenant-owned tables (DAT-005).

Each table gets two policies. A row is visible, and may be written, only when the transaction
names its tenant, or when the transaction says on purpose that it works across tenants:

    async with engine.begin() as connection:
        await in_tenant(connection, identity.tenant_id)
        ...  # only that tenant's rows exist here, even if a query forgets its WHERE

The policies are FORCEd, so they bind the table's owner, which is the service's own login.
They protect against a query that forgets its tenant. They cannot protect against code that sets
the setting itself, which is why `across_tenants` should be rare and easy to find.
"""

import re

from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection

TENANT_SETTING = "windops.tenant_id"
ALL_TENANTS_SETTING = "windops.all_tenants"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Not a safe SQL identifier: {name!r}")
    return name


def enable_tenant_isolation(connection: Connection, table: str, column: str = "tenant_id") -> None:
    """Call from a migration, on a table created in the same or an earlier revision."""
    if connection.dialect.name != "postgresql":
        raise RuntimeError("Row-level security needs PostgreSQL")
    table, column = _identifier(table), _identifier(column)
    mine = f"{column} = current_setting('{TENANT_SETTING}', true)"
    everyone = f"current_setting('{ALL_TENANTS_SETTING}', true) = 'on'"
    for statement in (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        # Without FORCE, the owner, which is the service's own login, would skip every policy.
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_one_tenant ON {table} USING ({mine}) WITH CHECK ({mine})",
        f"CREATE POLICY {table}_all_tenants ON {table} USING ({everyone}) WITH CHECK ({everyone})",
    ):
        connection.execute(text(statement))


async def in_tenant(connection: AsyncConnection, tenant_id: str) -> None:
    """Limit the rest of this transaction to one tenant's rows. Other databases ignore it."""
    if connection.dialect.name == "postgresql":
        # The last argument makes it last only until this transaction ends.
        await connection.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": TENANT_SETTING, "value": tenant_id},
        )


async def across_tenants(connection: AsyncConnection) -> None:
    """Let the rest of this transaction see every tenant. Use only for platform-wide work."""
    if connection.dialect.name == "postgresql":
        await connection.execute(
            text("SELECT set_config(:name, 'on', true)"), {"name": ALL_TENANTS_SETTING}
        )
