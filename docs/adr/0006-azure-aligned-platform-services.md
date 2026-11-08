# ADR 0006: Align the platform with Azure's reference architecture before M4.7

**Status:** Accepted, 2026-11-09

## Context

The finished system will be presented as an Azure-architected platform. We compared the design with the Azure
Architecture Center's references. The closest is the [advanced AKS microservices architecture][advanced], built on the
[AKS baseline][baseline], with the [Protect APIs][apis] pattern at the edge, where APISIX plays API Management's part.
Our design differed from those references in four places:

| Concern | The references | Ours until now |
| --- | --- | --- |
| Durable messaging | Azure Service Bus | NATS JetStream |
| A workload's identity to Azure services | Microsoft Entra Workload ID | Passwords and connection strings in Kubernetes Secrets |
| Network plugin and policy | Azure CNI powered by Cilium | Calico |
| Observability | Azure Monitor, managed Prometheus and Azure Managed Grafana, with Application Insights | Self-hosted Prometheus, Grafana, Loki and Tempo |

ADR 0005 put all Azure work in Phase 15, at the end. Changing these four then would reach into every service late in
the project. Doing it now, while there are still few services, costs less.

The system must keep running on the 16 GB laptop (ADR 0001), where Service Bus, Workload ID and Azure Monitor do not
exist, and Microsoft's Service Bus emulator needs about 2 GB more than the local profile has.

## Decision

- **Add Phase 4A, Azure alignment, before M4.7.** M4.7 waits until it is done.
- **Every Azure service sits behind the same interface as its local counterpart.** Settings choose which one runs,
  so a service's code is the same everywhere:

  | `WINDOPS_ENVIRONMENT` | Events | Identity to data stores | Telemetry |
  | --- | --- | --- | --- |
  | `local` | NATS JetStream | Passwords from Kubernetes Secrets | The self-hosted stack |
  | `dev`, `live` | Azure Service Bus | Entra Workload ID, with no stored password | Azure Monitor, managed Prometheus, Managed Grafana and Application Insights |

  `WINDOPS_EVENTS_BACKEND` can override the choice of events, so a laptop can be pointed at a real Service Bus to test
  it.
- **Service Bus:** a topic for each subject family, a subscription for each consumer, its native dead-lettering in
  place of our dead-letter stream, and the transactional outbox unchanged. The same event tests run against both
  back ends, and once against a real Service Bus namespace.
- **Workload ID** replaces stored passwords only for Azure resources: Service Bus, Azure Database for PostgreSQL and
  Key Vault. It does not replace our own service-to-service security. Every internal call still needs mutual TLS, a
  signed identity and a deadline.
- **Cilium** replaces Calico everywhere, locally too, as Minikube supports it. Network policies, from M13.3, are
  written for Cilium.
- **Azure Monitor:** the services keep exporting OpenTelemetry. On Azure, traces and logs go to Application Insights
  and Log Analytics, metrics to managed Prometheus, and our dashboards and alert rules are kept as code for Managed
  Grafana. Locally, the self-hosted stack stays.
- **Nothing is created in Azure without an approved plan with its monthly price.** This machine uses the Azure CLI's
  signed-in session, and CI uses GitHub OIDC (ADR 0005). No Azure credential is ever a setting or a file in the
  repository. The subscription ID, which is not secret but names the account, is `AZURE_SUBSCRIPTION_ID` in `.env`,
  not `.env.example`. Only the deployment scripts read it, so it is not a `WINDOPS_` setting and never reaches the cluster.

## Consequences

- Phase 4A has seven modules. Stage 1, the code, documents and local proof, costs nothing. Stage 2 creates real Azure
  resources to prove them, after the plan and price are approved.
- Phase 15 becomes smaller: the full deployment, hardening and the presentation. Its foundation, Service Bus,
  Workload ID and Azure Monitor work move earlier, into Phase 4A.
- Two event back ends must stay equivalent. A shared test suite keeps them so.
- Rebuilding the local cluster on Cilium removes its data. Everything on it is synthetic and can be seeded again.
- Kept as they are, as ADR 0005 decided: APISIX as the API gateway, behind Application Gateway WAF on Azure; Redis,
  Qdrant and Keycloak in the cluster; PostgreSQL with one database per service.

[advanced]: https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks-microservices/aks-microservices-advanced
[baseline]: https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks/baseline-aks
[apis]: https://learn.microsoft.com/en-us/azure/architecture/web-apps/api-management/architectures/protect-apis
