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
