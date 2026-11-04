# PostgreSQL

`windops-postgres` is the system of record (SRS 8.1), reachable only inside the cluster. It runs one
server, with a separate database and login per service
([ADR 0003](../../../docs/adr/0003-postgresql-without-timescaledb.md)).

## Service access

Each service gets a database and login named `windops_<service>`, such as `windops_fault_detection`.
A login can connect only to its own database, and has no administrator rights (ARC-002, ARC-003).

Its credentials are in the Secret `windops-<service>-database`, in that service's namespace only, with
the keys `host`, `port`, `database`, `username` and `password`. Services never receive the
administrator password.

## Add a service

1. Add it to `SERVICE_NAMESPACES` in `scripts/provision_postgres.py`.
2. Run `make secrets`, then `make postgres-provision`.

Both commands are safe to repeat, and keep existing passwords and data.

## Back up and restore

Back up one service's database to a file on your machine:

```bash
kubectl --context windops --namespace windops-data exec windops-postgres-0 -- \
  pg_dump --username=windops_admin --format=custom windops_audit > windops_audit.dump
```

Restore it, replacing that database's current contents:

```bash
kubectl --context windops --namespace windops-data exec --stdin windops-postgres-0 -- \
  pg_restore --username=windops_admin --dbname=windops_audit --clean --if-exists < windops_audit.dump
```

`make postgres-check` runs both commands against a test table. Scheduled backups arrive in M14.3
(DAT-003).

## Reset

This **deletes every service's data**. Back up anything you need first.

```bash
kubectl --context windops --namespace windops-data delete statefulset windops-postgres
kubectl --context windops --namespace windops-data delete pvc data-windops-postgres-0
make infra
```

`make infra` creates an empty server and all the databases again. `make clean` and
`make cluster-delete` also delete this data, because they remove its namespace. `make cluster-stop`
keeps it.

## Limits

- Connections are not encrypted until M3.3 adds mTLS.
- Any pod in the cluster can reach the port, though it still needs a password, until M13.3 adds
  network policies.
- A service's login owns its tables, so row-level security needs `FORCE ROW LEVEL SECURITY` when M3.6
  adds it.
