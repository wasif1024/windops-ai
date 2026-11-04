# Local Minikube cluster

WindOps runs in its own Minikube profile, `windops`. The commands use that profile and context
directly, so they do not change other Minikube clusters or your current `kubectl` context.

## Setup

Set Docker Desktop to at least 6 CPUs and 11 GB of memory, then run:

```bash
make cluster         # start the cluster with Calico and metrics-server
make secrets         # create namespaces and local secrets, using ignored files
make infra           # create the namespaces and etcd
make images          # build service images into the cluster
```

`make secrets` generates `secrets.local.*` files once. Git ignores them, so never commit them.

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
