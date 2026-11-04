# Microservices

The platform has 23 services, listed in [SRS section 7.2](srs.md#72-services). Each owns its data,
contracts, tests, image and manifests. Two sample services prove the platform until real ones exist.

## Create a service

```bash
make service NAME=telemetry-service
```

This creates the service from the template and adds it to the workspace, type checks and overlays.
Then follow the steps in its README.

## What every service gets

| Feature | Provided by | Requirement |
| --- | --- | --- |
| Rejects calls without a signed identity or a deadline | `windops-grpc` | GRP-003, GRP-004 |
| Maps errors to gRPC status codes, and hides internal details | `windops-grpc` | GRP-006 |
| Health endpoints, metrics and graceful shutdown | `windops-grpc` | GRP-006, DEP-004 |
| JSON logs, traces and redaction | `windops-observability` | OBS-001, OBS-003 |
| Breakers, bounded retries and deadlines on outgoing calls | `windops-resilience`, `ServiceClient` | SRS 7.7 |
| Tenant-scoped Redis cache and rate limits, with TTLs and a breaker | `windops-cache` | SRS 8.1, DAT-004, DAT-007 |
| Durable, tenant-carrying events, with retries and dead letters | `windops-events` | SRS 7.6, DAT-007 |
| Its own migration history and Job, for a service that stores data | `windops-migrations` | DAT-001, DAT-002 |
| Commits a state change and its event together, with a relay to publish it | `windops-outbox` | EVT-002, EVT-004 |
| Non-root image, and manifests with probes and memory limits | The service template | DEP-001 to DEP-005 |

## Call another service

1. Add or change the contract in `shared/protobuf`, and run `make contracts-gen`.
2. Depend on `windops-contracts`.
3. Call through `ServiceClient`, with one breaker for each dependency. The
   [sample caller](../services/sample-caller-service/src/windops_sample_caller/main.py) shows how.
4. Handle `BreakerOpenError` with the behavior SRS 7.7 defines for that dependency.

## Rules

- A service must not read or write another service's tables (ARC-002).
- Shared code holds contracts and cross-cutting libraries, never business logic (ARC-004).
- Readiness checks cover only what a service cannot work without. A dependency outage is the
  breaker's job, and must not make Kubernetes restart the caller (RES-006).

The [coding standards](coding-standards.md) have the full list.
