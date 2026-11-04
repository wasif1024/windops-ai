# Architecture

WindOps AI helps people investigate turbine faults and prepare maintenance. Agents recommend, and
deterministic services and authorized humans decide. The system never issues turbine commands.

The [README](../README.md#system-and-security-architecture) has the layer and security diagram, and
[SRS section 7](srs.md#7-microservices-grpc-events-and-circuit-breaking) has the service diagram.

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
