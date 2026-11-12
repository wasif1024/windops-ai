# Architecture

WindOps AI helps people investigate turbine faults and prepare maintenance. Agents recommend, and
deterministic services and authorized humans decide. The system never issues turbine commands.

The [README](../README.md#architecture) shows how the platform runs on Kubernetes, pod by pod. The detailed diagrams
are [below](#detailed-diagrams), and [SRS section 7](srs.md#7-microservices-grpc-events-and-circuit-breaking) has the
service diagram.

## How a request flows

This path runs today, using the sample services:

| Step | From | To | How | What protects it |
| --- | --- | --- | --- | --- |
| 1 | Client | API Gateway | HTTP, through a port-forward locally | Only entry point, with route timeouts and health checks |
| 2 | API Gateway | Sample caller | HTTP | Upstream health checks |
| 3 | Sample caller | Sample responder | gRPC | Signed identity, deadline, breaker and bounded retries |

A single trace covers all three steps, and the [Phase 1 exit gate](phases/phase-01-exit-gate.md) proves
it. Real requests will reach services through Web BFF, after M3 adds TLS and token checks at the gateway.

## Key decisions

| Decision | Why | Where |
| --- | --- | --- |
| LLMs never own thresholds, severity, money or approvals | Safety-critical logic must be deterministic and testable | SRS 2.1 |
| gRPC and versioned Protobuf between services | Typed contracts that cannot break by accident | [gRPC contracts](grpc-contracts.md) |
| One owner for each kind of state | Avoids competing sources of truth | SRS 5.5 |
| JetStream and a transactional outbox for durable events | State and its event commit together | SRS 7.6 |
| A local profile for a 16 GB laptop | Everything runs locally with no feature removed | [ADR 0001](adr/0001-local-minikube-profile.md) |
| Tempo for trace storage | The SRS required traces, but listed no store | [ADR 0002](adr/0002-trace-storage-and-kube-state-metrics.md) |

## Where to read more

- [Service map](service-map.md): how all 23 services connect, and what happens when one fails
- [Microservices](microservices.md): how services are built
- [Deployment](deployment.md): how to run the system
- [Observability](observability.md): logs, traces, metrics and dashboards
- [Implementation tracker](phases/README.md): what is built, and what comes next

## Detailed diagrams

### Every service, and the security on each hop

[![WindOps AI platform architecture: AI-assisted fault investigation for renewable assets. Operators reach the React frontend, and the telemetry simulator sends device samples, both over HTTPS through the APISIX API Gateway, which checks sign-in with Keycloak. In the windops-system namespace, Web BFF calls the Identity, Authorization and Tenant services over gRPC. In windops-application, the Agent Runtime runs a Supervisor and five specialist agents, which reach services only through four MCP servers in windops-mcp. Twenty domain services, grouped by capability, call each other over gRPC with mutual TLS, a signed identity and circuit breakers, and publish durable events through four streams: NATS JetStream locally, and Azure Service Bus topics on Azure. Each service owns its PostgreSQL database with row-level security, alongside Redis and Qdrant in windops-data, and external providers are reached through breakers. Every workload sends logs, metrics and traces to the observability stack: Loki, Prometheus, Tempo and Grafana locally; Log Analytics, managed Prometheus, Application Insights and Managed Grafana on Azure. An Azure panel shows what the platform runs on in Sweden Central: Application Gateway WAF, AKS powered by Cilium, Service Bus, Azure Database for PostgreSQL, Key Vault, Entra Workload ID, Container Registry and Azure Monitor. A side column lists the security controls on each hop, in place or planned with their module, the circuit breaker's states and failure behaviors, the agent guardrails, where the system runs and its monthly Azure cost. A strip shows delivery progress across every phase.](assets/platform-architecture.png)](assets/platform-architecture.png)

<sub>A green dot marks a service running today. Security controls marked planned name the module that delivers them;
tests fail if a control is still shown as planned once its modules are finished, so the diagram cannot drift from the
[implementation tracker](phases/README.md).</sub>

### The staging deployment on Azure

[![WindOps AI on Azure, the staging deployment in Sweden Central. Delivery: GitHub Actions checks every pull request, and after a merge Azure Pipelines validates with a what-if, waits for an approval of the cost, deploys, deploys the platform onto AKS, proves it and always removes the compute; a nightly pipeline stops PostgreSQL when no cluster uses it. The pipeline signs in by workload identity federation, with no stored secret. In the subscription, the compute resource group, about $1.20 an hour and deleted after each run, holds Application Gateway WAF v2 in Prevention mode, re-encrypting to APISIX, and AKS 1.35 with a D2ds_v4 system pool and a D4ds_v4 user pool, and Managed Grafana. Its namespaces show what the pipeline deploys now, Authorization, Tenant, Asset, Audit, the sample caller and responder, the database setup Job and the Collector, and what the full deployment adds in Phase 15, APISIX, Keycloak, Redis and Qdrant. Add-ons include the Workload ID webhook, the Key Vault CSI driver, managed Prometheus, Defender, Azure Policy, Image Cleaner and cert-manager issuing from the CA in Key Vault. The data resource group, always there for about $20 a month, holds the VNet and its subnets, Azure Database for PostgreSQL with private access and Entra sign-in only, Service Bus Standard, Key Vault, Container Registry, Application Insights with Log Analytics, the Monitor workspace that Grafana reads, one managed identity per workload, and the budget with its alerts. A panel explains how a pod signs in with no secret. A side table compares each concern locally and on Azure, and a cost table lists idle, proof run, demo and always-on prices.](assets/azure-architecture.png)](assets/azure-architecture.png)

<sub>What the pipeline deploys today, by resource group, with each concern locally and on Azure and the cost. All three
diagrams are generated by `uv run python -m scripts.generate.readme_visuals`.</sub>
