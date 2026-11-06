# Observability

Every request can be followed through logs, traces and metrics. A service gets all three from
the shared libraries, without extra code. Agent observability through LangSmith arrives in M8.9.

## How data flows

| Signal | Written by | Carried by | Stored in | Viewed in |
| --- | --- | --- | --- | --- |
| Logs | JSON on stdout | Fluent Bit | Loki | Grafana |
| Traces | OpenTelemetry | OTel Collector | Tempo | Grafana |
| Metrics | `/metrics` on each pod | Scraped by Prometheus | Prometheus | Grafana |
| Alerts | Prometheus rules | Prometheus | Alertmanager | Alertmanager |

Logs and traces share a `trace_id`, so a log line leads to its trace.

## Reach it locally

```bash
make observability-forward   # Grafana at http://127.0.0.1:3000
```

Sign in as `admin`. The password is in `infrastructure/minikube/secrets.local.grafana.env`, which
`make secrets` generates.

## Dashboards

| Dashboard | Shows |
| --- | --- |
| Platform Health | A health table with each pod's status, restarts, memory and last stop reason, then CPU, memory, restarts, pods not ready, scrape targets down, and data store readiness, memory and restarts |
| Microservice Health | Request rate, latency percentiles, gRPC failures and breaker state |
| [Events](events.md) | Consumer lag, outbox backlog, dead letters and stream storage |
| [Security](security.md#seeing-it-the-security-dashboard) | Failed sign-ins, wrong passwords, MFA failures, access denials, rate limiting, refused tokens and administrator changes |

The other SRS 10.3 dashboards arrive with the components they describe. Panels stay empty until something produces the
data, such as a breaker opening.

## Policies by environment (OBS-004)

| Policy | `local` | `ci` | Production-style |
| --- | --- | --- | --- |
| Log retention | 7 days in Loki | No stack in CI | Set from the operator's requirements |
| Metric retention | 7 days or 1 GB in Prometheus | No stack in CI | Set from the operator's requirements |
| Trace retention | 24 hours in Tempo | No stack in CI | Set from the operator's requirements |
| Log rotation | The container runtime's defaults, then Loki | Runner logs | The runtime's defaults, then Loki |
| Access | Grafana login, reached only through `kubectl port-forward` | Not applicable | Single sign-on and role mapping, added in Phase 3 |
| Redaction | Applied by the shared library before logs and spans leave a pod | The same | The same |

Metric attributes are not redacted, so they must hold only low-risk values. Loki and Prometheus have
no authentication of their own locally. Nothing outside the cluster can reach them, and M13.3 limits
access inside it with NetworkPolicies.

## Limits

- The stack keeps data on one node, which suits local work only (SRS 2.4).
- Alerts reach Alertmanager, but nothing sends them to a person yet. M13.7 adds the remaining SRS 10.3 alerts
  and routing.
- Data store health comes from each store's readiness probe, memory and restarts. Query rates and
  database-level figures would need an exporter per store, each with its own login, which is not justified yet.
- Fluent Bit runs as root, because it reads other containers' log files.
- It does not collect the stack's own logs, to keep a failing store from causing a feedback loop.
- Tempo and kube-state-metrics are additions to the SRS, recorded in
  [ADR 0002](adr/0002-trace-storage-and-kube-state-metrics.md).
