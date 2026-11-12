# Phase 4A exit gate: the platform proven on Azure

**Result:** passed on 2026-11-12. The local gate ran on the Minikube cluster, rebuilt on Cilium. The Azure proof
ran in the Azure DevOps pipeline, which deployed the `staging` environment in Sweden Central from `main` at
`5c5abd8`, then removed the compute.

Phase 4A ([ADR 0006](../adr/0006-azure-aligned-platform-services.md)) aligns the platform with Azure's
reference architecture. It is done when the phase gates pass again on Cilium, and when Service Bus, Workload
ID and monitoring are proven on real Azure resources (M4A.6).

## On Azure: one pipeline run

The run's stages, as [docs/azure-devops.md](../azure-devops.md) describes them:

| Stage | Result |
| --- | --- |
| Validate | The Azure tests, Azure's preflight validation, the vCPU quota check and a `what-if` passed. Nothing was created |
| Deploy | After approval: the data group, then AKS, Application Gateway WAF v2 and Managed Grafana, and each workload's federated identity |
| Platform | Six images built and pushed, settings written to Key Vault, cert-manager issuing from the CA in Key Vault, the Collector, each service's Entra login and database, four services' migrations, then six services |
| Prove | Every check passed (below) |
| Remove compute | The cluster, Application Gateway and Grafana deleted, and PostgreSQL stopped |

Prove's output:

```text
ok - open_events connects to Service Bus, from settings
ok - and the event it published is delivered to a subscription
ok - Service Bus: An event is delivered to its consumer once
ok - Service Bus: The same event sent twice is delivered once
ok - Service Bus: A handler that keeps failing has the event dead-lettered, as failed
ok - Service Bus: Something that is not an event is dead-lettered at once, as invalid
ok - Service Bus: A handler whose dependency is down is asked again, never dead-lettered
Service Bus check passed: the same contract as NATS
PASS 2 AKS nodes are Ready
PASS 6 services are Available, with their Secrets from Key Vault
PASS windops-asset's migrations ran, signed in to PostgreSQL by Entra
PASS windops-audit's migrations ran, signed in to PostgreSQL by Entra
PASS windops-authorization's migrations ran, signed in to PostgreSQL by Entra
PASS windops-tenant's migrations ran, signed in to PostgreSQL by Entra
PASS No Workload ID pod is given a password (12 pods)
PASS The caller reached the responder over mutual TLS with a signed identity
PASS Application Insights has the echo's trace (… requests)
```

The last two lines are the checks #88 fixed. They passed in the green run, and the trace they found is below.

The echo's trace in Application Insights: the caller's request and the responder's gRPC call share one
operation, so a single trace follows the request across both services.

| Time (UTC) | Service | Operation | Result | Operation ID |
| --- | --- | --- | --- | --- |
| 13:54:56.153 | sample-caller | `GET /sample/echo` | OK | `5e6273bbb269b3a0e036b6043e8aaae7` |
| 13:54:56.164 | sample-responder | `grpc /windops.sample.v1.SampleResponderService/Echo` | OK | `5e6273bbb269b3a0e036b6043e8aaae7` |

All six services also sent their own traces: about 1,200 requests and 3,700 dependencies in the hour.

### What each check proves

| ADR 0006 asks for | Proven by |
| --- | --- |
| Azure Service Bus behind the same events interface as NATS | The five-scenario contract, unchanged, against the deployed namespace, signed in with Entra and no keys |
| Workload ID to PostgreSQL, with no stored password | A Job signed in as PostgreSQL's Entra administrator made each service's login by object ID. Each service's migrations then signed in as that service |
| Workload ID to Key Vault | Every service became Available only after the Secrets Store CSI driver read its Secrets from Key Vault |
| No stored secrets | No Workload ID pod has a database or events password in its settings |
| Our own service security, kept on Azure | cert-manager issued each service's certificate from the CA kept in Key Vault, and the caller called the responder over mutual TLS with a signed identity |
| Azure Monitor | Traces from every service reached Application Insights through the Collector, and container logs reached Log Analytics |

### What the first runs found

Each of these appeared only when Azure created a real resource. Each was fixed in its own pull request, and the
ones that can be checked earlier now fail the free Validate stage:

| Pull request | What Azure refused | The fix, and how it is caught now |
| --- | --- | --- |
| #82 | The pipeline's variables were ignored | Every variable group setting is in `.env.example`, held by a test |
| #83 | Managed Grafana's name was 25 characters, over 23, and the Essential plan is not offered | Grafana Standard in the compute group. `plan` runs Azure's preflight validation, and a test checks every name's length |
| #84 | Burstable PostgreSQL is refused to new subscriptions in Europe, and alert rules must wait at least a minute | General Purpose D2ds_v5, and rules wait one minute, held by a test |
| #85 | No vCPU quota for any v5 VM family | Ddsv4 nodes, and a quota check before anything is created |
| #86 | Application Gateway could not read its certificate past Key Vault's firewall | Trusted services may pass, and each still needs a role |
| #87 | `sslrootcert=system` found no roots in psycopg's own OpenSSL | Verified against certifi's public roots, held by a test |
| #88 | The proof called the sample caller's absent HTTPS port | The caller's HTTP port, as the gateway's route uses |

`make azure-logs` reads a deleted cluster's container logs from Log Analytics. #87's error came from there.

## Locally: the phase gates again, on Cilium

| Check | Result |
| --- | --- |
| `make check`: lint, types, contracts and 1,497 tests; frontend; dependency audits | Passed |
| `make cluster-check`: Cilium enforces Kubernetes and Cilium network policies | Passed |
| `make phase-1-gate`, `make phase-2-gate`, `make phase-3-gate` | Passed, after #81 fixed three checks and one dashboard bug |
| Phase 4 so far: `asset-check`, `telemetry-check`, `simulator-check`, `telemetry-storage-check`, `cms-check`, `data-quality-check` | Passed |

## Not yet on Azure

Application Gateway is created, but traffic reaches APISIX through it only once M15.7 adds APISIX's internal load
balancer. Identity, telemetry, the Web BFF, the gateway and the frontend need Keycloak, Redis or etcd in the
cluster, which the full deployment brings in Phase 15.
