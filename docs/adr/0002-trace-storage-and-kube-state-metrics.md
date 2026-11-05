# ADR 0002: Add Tempo for traces, and kube-state-metrics

**Status:** Accepted, 2026-11-04

## Context

SRS 13.1 requires distributed traces to be available locally, and the Phase 1 exit gate requires a
traced call. SRS 7.1 and 10.1 list OpenTelemetry, which carries traces, but no store for them:
Prometheus holds metrics, and Loki holds logs.

The Platform Health dashboard in SRS 10.3 needs pod restarts and readiness. Prometheus cannot get
those from the container metrics alone.

## Decision

- Add Grafana Tempo as `windops-tempo`, keeping traces for 24 hours on a small volume.
- Add `windops-kube-state-metrics`, with read-only access to pods and workloads.
- Have Prometheus scrape each pod's `/metrics`, as SRS 7.1 describes. Services send only traces
  through the collector, so metrics are not stored twice.

Tempo was chosen over Jaeger because it needs no separate database and works directly with Grafana,
which keeps logs, traces and metrics in one place.

## Consequences

- Two workloads more than SRS 11.2 listed, using far less than their limits when idle. Tempo needs a 512 MiB limit, and small, frequent blocks, because writing a block of traces briefly needs much more memory. Measured figures
  are in `infrastructure/minikube/README.md`.
- Grafana can follow a log line to its trace through the shared `trace_id`.
- Local traces last only 24 hours. Longer retention needs object storage, which is out of scope.
