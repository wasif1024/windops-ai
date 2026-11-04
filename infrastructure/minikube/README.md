# Local Minikube cluster

WindOps runs in its own Minikube profile, `windops`. The commands use that profile and context
directly, so they do not change other Minikube clusters or your current `kubectl` context.

## Setup

Set Docker Desktop to at least 6 CPUs and 11 GB of memory, then run:

```bash
make cluster         # start the cluster with Calico and metrics-server
make infra           # create the namespaces
make secrets         # create local secrets from an ignored file
make images          # build service images into the cluster
```

`make secrets` generates `secrets.local.env` once. Git ignores it, so never commit it.

## Day to day

| Command | What it does |
| --- | --- |
| `make cluster-stop` | Stops the cluster and frees its memory, and keeps its data |
| `make cluster` | Starts it again |
| `make cluster-check` | Deploys a throwaway service to prove the manifests work |
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
