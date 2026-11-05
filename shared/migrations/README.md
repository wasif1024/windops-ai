# windops-migrations

Runs each service's Alembic migrations, the same way for every service.

| Rule | How it is enforced | Requirement |
| --- | --- | --- |
| Each service owns its history | Migrations live in `src/<package>/migrations`, and use that service's own database and login | DAT-001 |
| Migrations run as a Job, never at startup | Only `make migrate` runs them, and a test keeps them out of the service's startup code | DAT-002 |
| Changes follow expand/contract | A test fails if a revision makes a breaking change without saying so | DAT-001 |
| Two Jobs cannot collide | The second waits on a database lock, then finds nothing left to do | DAT-002 |
| A failed migration changes nothing | Each revision runs in one transaction | DAT-002 |

## Write a migration

```bash
make migration PACKAGE=windops_audit MESSAGE="add audit records"
```

This adds a file under `services/audit-service/src/windops_audit/migrations/versions/`. Fill in `upgrade()`.

## Expand and contract

Old and new versions of a service run side by side during a rolling update, so the database must work with
both. Each revision declares its `phase`:

| Phase | What it may do | When it runs |
| --- | --- | --- |
| `expand` | Add tables, nullable columns, columns with a default, and indexes | Before the new version rolls out |
| `contract` | Drop, rename or tighten what no running version still uses | In a later release |

To rename a column, for example:

1. **Release 1, expand:** add the new column, and have the code write to both.
2. **Release 2, expand:** copy the old values across, and read the new column.
3. **Release 3, contract:** drop the old column.

A test catches a breaking operation in an `expand` revision. It cannot tell whether a `contract` revision
is safe yet, so a reviewer checks that no running version still uses what it removes.

## Row-level security

Every tenant-owned table needs PostgreSQL row-level security (DAT-005). Turn it on in a migration, and name the
tenant in every transaction:

```python
from windops_migrations import enable_tenant_isolation, in_tenant


def upgrade() -> None:
    enable_tenant_isolation(op.get_bind(), "readings")


async with engine.begin() as connection:
    await in_tenant(connection, identity.tenant_id)
```

The policies are FORCEd, so they bind the service's own login, which owns the tables. A query that forgets
its `WHERE tenant_id = ...` sees one tenant's rows, and a write that would move a row to another tenant is
refused. `across_tenants` lets a transaction see every tenant, for the rare platform-wide read. Naming a
tenant afterwards ends it.

| When | How |
| --- | --- |
| A new table | Turn it on in the `expand` revision that creates it, before any code reads it |
| An existing table | First release code that names the tenant, then turn it on in a later `contract` revision, because code that does not name one sees nothing |

`tests/test_row_level_security.py` fails if a migration creates a table with a `tenant_id` and never turns it
on. `make cross-tenant-check` asks PostgreSQL the same on the cluster, and attacks each table.

Row-level security stops a query that forgets its tenant. It does not stop code that names a tenant on
purpose, or the service's own login used by hand.

## Run migrations

```bash
make migrate
```

This runs each service's Job in turn, and stops at the first failure. Run it once per release, before `make
services`. Running it again changes nothing.

## Limits

- `downgrade()` is not used. To undo a release, write a new forward migration, or restore a backup.
- A long-running migration holds its table lock for its whole transaction, and gives up after 10 seconds if
  it cannot get one. Split large data changes into small batches.
