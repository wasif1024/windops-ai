# ADR 0005: Deploy to Azure as the final phase

**Status:** Accepted, 2026-11-05

## Context

SRS v1.1 targets Minikube, and says only that the manifests are portable to other clusters. The project's
owner also wants to show cloud deployment work, so the finished system needs to run on Azure.

Running a cloud cluster now would cost hundreds of dollars a month, with little real to run on it.

## Decision

- Add Phase 15, after hardening, to deploy the finished system to Azure.
- Keep Minikube as the working environment until then, and for every earlier exit gate.
- Use these services, subject to the decisions in M15.1:

| Need | Choice | Why |
| --- | --- | --- |
| Kubernetes | Azure Kubernetes Service, with the existing manifests | No separate virtual machines to manage |
| Images | Azure Container Registry | The pipeline pushes signed images here |
| Secrets | Azure Key Vault | Replaces the local secret files |
| Web application firewall | Application Gateway WAF v2, in front of APISIX | APISIX stays the only way into the cluster |
| PostgreSQL | Azure managed PostgreSQL | Provides the backups and standby option in ADR 0003 |
| Redis, Qdrant, NATS | Stay in the cluster | Already built and tested, and cheaper |
| Monitoring | The existing Grafana and Prometheus stack | Already built |
| Infrastructure code | Bicep, kept in this repository | Azure's own tool, with no state file to protect |
| Delivery | GitHub Actions with OIDC login and a manual approval | The repository is public, so it stores no Azure password |

Multi-region recovery and high-availability SLAs stay out of scope (SRS 2.4).

## Consequences

- Earlier work should stay portable, and call out anything that would make this phase harder.
- The system must not rely on Minikube-only behavior, such as local image loading, beyond its overlay.
- Azure costs real money. M15.1 sets a budget alert before anything is created, and the system can be stopped
  between demonstrations.
- Before this phase is planned in detail, prices and available quota must be checked again. On 2026-11-05 a
  rough estimate was about $670 to $700 a month for a public, hardened deployment running all the time.
- A cloud deployment is new scope beyond SRS v1.1, recorded here and in SRS section 12.
