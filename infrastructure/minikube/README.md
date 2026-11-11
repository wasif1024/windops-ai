# Local Minikube cluster

WindOps runs in its own Minikube profile, `windops`. The commands use that profile and context
directly, so they do not change other Minikube clusters or your current `kubectl` context.

## Setup

Set Docker Desktop to at least 6 CPUs and 11 GB of memory, then run:

```bash
make cluster         # start the cluster with Cilium and metrics-server
make secrets         # create namespaces and local secrets, using ignored files
make infra           # create the namespaces, cert-manager, etcd, data stores, Keycloak and observability
make images          # build service images into the cluster
```

`make secrets` generates `secrets.local.*` files once. Git ignores them, so never commit them.

`make infra` and `make services` also put the settings from `.env` into the `windops-settings` ConfigMap, the same
as `make config`. See [Settings and provider keys](../../README.md#settings-and-provider-keys). `make dev` runs all
of this the first time.

## Day to day

| Command | What it does |
| --- | --- |
| `make cluster-stop` | Stops the cluster and frees its memory, and keeps its data |
| `make cluster` | Starts it again |
| `make cluster-check` | Deploys a throwaway service to prove the manifests work |
| `make gateway-check` | Proves the API gateway works, using a throwaway service |
| `make clean` | Removes WindOps workloads and namespaces, and keeps the cluster |
| `make cluster-delete` | Deletes the cluster and all its data |

## Overlays

| Overlay | Used for | Replicas |
| --- | --- | --- |
| `local` | This laptop | 1 |
| `ci` | Automated checks | 1 |
| `production-example` | An example for a larger cluster, not a tested deployment | 2 |

## Measured memory

These figures replace estimates as workloads are added. See
[ADR 0001](../../docs/adr/0001-local-minikube-profile.md) for the size decision.

| Date | What was measured | Memory | Notes |
| --- | --- | --- | --- |
| 2026-11-04 | Idle cluster: Kubernetes, Calico and metrics-server | 1.4 GiB of 10 GiB | From `docker stats` |
| 2026-11-09 | New cluster on Cilium, a minute after it started, before any workload (ADR 0006) | 560 MiB of 10 GiB | From `docker stats`, before metrics-server and Cilium had settled |
| 2026-11-04 | One generated service, running with no real work | 48 MiB | From `kubectl top` |
| 2026-11-04 | APISIX gateway, with no real traffic | 50 MiB | From `kubectl top` |
| 2026-11-04 | etcd, with no real traffic | 27 MiB | From `kubectl top` |
| 2026-11-04 | Whole cluster with the gateway and etcd | 1.68 GiB of 10 GiB | From `docker stats` |
| 2026-11-04 | Grafana, the largest observability workload | 323 MiB | From `kubectl top` |
| 2026-11-04 | Prometheus, Loki, Tempo, Collector, Fluent Bit, Alertmanager and kube-state-metrics | 335 MiB in total | From `kubectl top`, with almost no data stored |
| 2026-11-04 | Whole cluster with the observability stack and etcd, and no gateway | 2.07 GiB of 10 GiB | From `docker stats` |
| 2026-11-04 | One sample service, with almost no work | 47 MiB | From `kubectl top` |
| 2026-11-04 | Whole cluster with the stack, gateway and 2 sample services | 2.31 GiB of 10 GiB | From `docker stats` |
| 2026-11-04 | Tempo while writing stored traces | Over 320 MiB, so its limit is 512 MiB | Killed at 320 MiB after a restart |
| 2026-11-05 | PostgreSQL, with 20 empty service databases | 32 MiB | From `kubectl top`, with almost no connections |
| 2026-11-05 | Whole cluster with the stack, gateway, 2 sample services and PostgreSQL | 2.84 GiB of 10 GiB | From `docker stats` |
| 2026-11-05 | Redis, with an empty cache | 11 MiB | From `kubectl top` |
| 2026-11-05 | Whole cluster with the stack, gateway, 2 sample services, PostgreSQL and Redis | 2.78 GiB of 10 GiB | From `docker stats` |
| 2026-11-05 | Qdrant, with no collections | 126 MiB | From `kubectl top` |
| 2026-11-05 | Whole cluster with the stack, gateway, 2 sample services, PostgreSQL, Redis and Qdrant | 2.77 GiB of 10 GiB | From `docker stats` |
| 2026-11-05 | NATS JetStream, with empty streams | 8 MiB | From `kubectl top` |
| 2026-11-05 | Whole cluster with the stack, gateway, 2 sample services and all four data stores | 2.93 GiB of 10 GiB | From `docker stats` |
| 2026-11-05 | Audit Service, with almost no work | 82 MiB | From `kubectl top` |
| 2026-11-05 | Whole cluster with the stack, gateway, 3 services and all four data stores | 2.97 GiB of 10 GiB | From `docker stats` |
| 2026-11-05 | NATS metrics exporter | 7 MiB | From `kubectl top` |
| 2026-11-05 | Tempo, after a restart with stored traces | 455 MiB of its 512 MiB limit | From `kubectl top`, so it may need a higher limit |
| 2026-11-05 | Whole cluster with the stack, gateway, 3 services, all four data stores and the exporter | 2.49 GiB of 10 GiB | From `docker stats` |
| 2026-11-05 | Tempo, writing a block every 5 minutes for 37 minutes | Peak 405 MiB of its 512 MiB limit | From Prometheus, with no restart |
| 2026-11-05 | Whole cluster after every Phase 2 check | 3.07 GiB of 10 GiB | From `docker stats` |
| 2026-11-05 | cert-manager, with its webhook and cainjector | 71 MiB across 3 pods | From `kubectl top` |
| 2026-11-05 | Keycloak, with almost no logins | 473 MiB of its 1 GiB limit | From `kubectl top` |
| 2026-11-05 | Whole cluster with the stack, gateway, 3 services, all four data stores and Keycloak | 3.66 GiB of 10 GiB | From `docker stats` |
| 2026-11-11 | Whole system on Cilium: every service, the simulator, the data stores and observability | 6.26 GiB of 10 GiB | From `docker stats`, with the simulator streaming |
