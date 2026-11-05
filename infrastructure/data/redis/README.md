# Redis

`windops-redis` holds cache entries and rate-limit counters, and is reachable only inside the cluster.
It saves nothing to disk, so every restart starts empty. Anything that must survive a restart belongs in
PostgreSQL or JetStream (SRS 8.1, EVT-005).

## Service access

Each service has its own Redis login, in the Secret `windops-<service>-cache` in its namespace, with the
keys `host`, `port`, `username` and `password`. A login can reach only keys starting `<service>:`, and can
run only the few commands the cache needs. The default login is off, and the `windops_admin` password is
kept in `windops-data`.

Services use [windops-cache](../../../shared/cache/README.md), which also requires a TTL and a tenant on
every key.

## Add a service

1. Add it to `SERVICE_NAMESPACES` in `scripts/provision_postgres.py`.
2. Run `make secrets`.
3. Run `kubectl --context windops --namespace windops-data rollout restart deployment/windops-redis`, so
   Redis loads the new login. This empties the cache, which is safe.

## Reset

Run the restart command above. There is nothing to back up.

## When Redis is down

Cache reads act as misses, so work continues from the source service, more slowly. Rate limits refuse
requests on protected routes, because they cannot be checked (SRS 7.7).

`make redis-check` proves all of this on the cluster, including a real outage.

## Limits

- Connections use TLS, and clients check the server's certificate, but a client proves who it is with a
  password and not a certificate. See [Service certificates](../../security/pki/README.md).
- A login is limited by service, not tenant. `windops-cache` adds the tenant to each key, so a tenant
  boundary relies on that library.
- At the 64 MB limit, the least recently used entries are removed first.
