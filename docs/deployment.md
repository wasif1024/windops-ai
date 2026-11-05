# Deployment

The system runs on Kubernetes, with Minikube as the local target. Set up the local cluster using
[infrastructure/minikube](../infrastructure/minikube/README.md).

## Bootstrap

SRS 11.4 defines this order:

```bash
make cluster          # start the cluster
make images           # build service images into the cluster
make secrets          # create namespaces and local secrets
make infra            # create cert-manager, etcd, data stores, Keycloak and observability
make identity-seed    # create the Keycloak realm, tenants and demo users
make migrate          # run each service's migration Job
make services         # deploy the gateway and services
make gateway-routes   # apply the gateway's routes
```

`make seed-demo` and `make smoke-test` arrive with their
modules. `make help` lists what each one does, and which module delivers it.

## Check it

| Command | What it proves |
| --- | --- |
| `make scaffold-check` | The service template builds, runs and shuts down cleanly |
| `make cluster-check` | Manifests, Pod Security, NetworkPolicy and graceful shutdown work on the cluster |
| `make gateway-check` | The gateway routes, skips unhealthy backends, and keeps routes through restarts |
| `make observability-check` | One call appears in logs, traces and metrics, and dashboards work |
| `make sample-check` | Two services work together, through the gateway |
| `make postgres-check` | Services are isolated in PostgreSQL, and data survives a restart and a restore |
| `make redis-check` | Services are isolated in Redis, nothing survives a restart, and an outage is handled safely |
| `make qdrant-check` | Qdrant needs a key, and vectors survive a restart and a restore |
| `make nats-check` | Events survive a restart, only a subject's owner can publish it, failures reach the dead-letter stream, and an outage is reported |
| `make migrate-check` | Migration Jobs apply once, repeat safely, undo a failure, and cannot collide |
| `make outbox-check` | A state change and its event commit together, wait out a JetStream outage, and are not stored twice |
| `make audit-check` | A repeated event makes one record, records cannot change, tampering is detected, and outages lose nothing |
| `make events-check` | Real failures appear on the Events dashboard, and their alerts fire |
| `make identity-check` | Keycloak signs in through OIDC, issues the tenant claim, and enforces its rules |
| `make mfa-check` | Administrators must use a second factor, step-up works, and recovery codes work. Takes about 7 minutes |
| `make identity-service-check` | Users are created, disabled and signed out in their own tenant only, and refused when Authorization is down |
| `make authorization-check` | Roles, scoped grants, step-up, break-glass and audited denials work |
| `make data-tls-check` | Each data store refuses plain connections, and loads a renewed certificate with no restart |
| `make mtls-check` | Only services with a certificate from our CA can connect, and a renewed certificate is picked up |
| `make phase-1-gate` | A call is traced through the gateway, and its breaker opens under a fault |
| `make phase-2-gate` | An event survives a pod restart, and a redelivered audit event creates only one record |

## Data

[PostgreSQL](../infrastructure/data/postgres/README.md) describes the service databases, and how to
reset, back up and restore them.

[Redis](../infrastructure/data/redis/README.md) describes the cache, and what happens when it is down.

[Qdrant](../infrastructure/data/qdrant/README.md) describes vector storage, and how to reset, back up and
restore it.

[Keycloak](../infrastructure/security/keycloak/README.md) describes login, tenants and token rules.

[NATS JetStream](../infrastructure/data/nats/README.md) describes the event streams, who can publish them,
and what happens when it is down.

## Environments

| Overlay | Used for | Replicas |
| --- | --- | --- |
| `local` | A 16 GB laptop | 1 |
| `ci` | Automated checks | 1 |
| `production-example` | An example for a larger cluster, not a tested deployment | 2 |

Set `OVERLAY` to choose one, as in `make services OVERLAY=ci`.

## Limits

- CI renders the manifests, but has no cluster. The checks above run locally.
- The project is a portfolio-grade platform on synthetic data, and is not certified for production use
  (SRS 1).
