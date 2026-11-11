# Azure deployment plan

The plan for M4A.6: what WindOps AI runs on in Azure, its size and its price, and how to stop paying for it. It follows
[ADR 0006](adr/0006-azure-aligned-platform-services.md): the AKS baseline, the advanced AKS microservices
architecture, and Application Gateway with a WAF in front of the API gateway.

**Status: awaiting approval.** Nothing in this plan exists in Azure yet. M4A.6 writes it in Bicep, shows a `what-if`
of every change, and creates it only after the plan and price are approved.

Prices are pay-as-you-go list prices in USD, excluding tax, from the Azure Retail Prices API on 2026-11-11, for
Sweden Central. A month is 730 hours.

## The four goals, and how the plan meets them

| Goal | How |
| --- | --- |
| **Cost-effective** | Compute runs only while it is used. It lives in its own resource group, created for a demo and deleted after, so an idle month costs about **$19**. Burstable PostgreSQL, Free-tier AKS, no self-hosted monitoring, and a budget alert |
| **Reliable** | Managed services for state: Service Bus, PostgreSQL with point-in-time restore, Key Vault with purge protection. Everything is Bicep, so the compute comes back the same each time. The services' own breakers, retries and outbox are unchanged |
| **Secure** | No stored secrets: every workload signs in with Entra Workload ID. A WAF in front of the gateway, PostgreSQL with no public address and no passwords, local auth off on Service Bus and ACR, and Defender for Containers |
| **Efficient** | Managed Prometheus, Application Insights and Log Analytics replace six self-hosted pods. Ephemeral OS disks, a cluster autoscaler, and collection limited to what the dashboards and alerts use |

## Region

**Sweden Central.** Of the regions compared, it was the cheapest for nearly every service, and it has availability
zones. Its grid is also almost fossil-free, which suits a renewable-energy product.

| Hourly price | Sweden Central | UAE North | East US 2 |
| --- | --- | --- | --- |
| D4as_v5 node | $0.184 | $0.212 | $0.356 |
| PostgreSQL B2s | $0.040 | $0.079 | n/a |
| App Gateway WAF v2, fixed | $0.468 | $0.475 | n/a |

UAE North is closer to the Gulf, but compute costs about 15% more and PostgreSQL twice as much. The region is a
single Bicep parameter, so changing it later is one line.

## What gets created

Two resource groups, so a demo's compute can be deleted without touching data.

```text
rg-windops-data      exists the whole time: about $19 a month
├── vnet-windops       10.40.0.0/16, its subnets and network security groups
├── Service Bus        sb-windops-dev, Standard
├── PostgreSQL         psql-windops-dev, Flexible Server B2s, private access only
├── Key Vault          kv-windops-dev, RBAC, purge protection
├── Container Registry acrwindopsdev, Basic
├── Log Analytics      log-windops-dev, with Application Insights on top
├── Azure Monitor      amw-windops-dev, the managed Prometheus workspace
├── Managed Grafana    amg-windops-dev, Essential
├── Managed identities one per workload, federated to its Kubernetes service account
└── Budget             $100 a month, with alerts

rg-windops-compute   exists only while the platform runs: about $0.98 an hour
├── AKS                aks-windops-dev, Free tier, Azure CNI Overlay powered by Cilium
│   ├── system pool    1 × Standard_D2ads_v5
│   └── user pool      1 × Standard_D4ads_v5, autoscaling to 2
├── App Gateway        agw-windops-dev, WAF v2, in front of APISIX
└── Public IPs         the gateway's address, and the cluster's outbound address
```

### Data resource group: about $19 a month, running or not

| Resource | Size and settings | Per month |
| --- | --- | --- |
| Service Bus | Standard: 3 topics, filtered subscriptions, dead-letter queues. The first 13 million operations are included | $9.81 |
| PostgreSQL storage | 32 GiB, with 7 days of backups included at no extra cost | $4.38 |
| Container Registry | Basic, with 10 GiB of images included | $5.07 |
| Key Vault | Standard, at $0.03 per 10,000 operations | under $0.50 |
| Log Analytics and Application Insights | Pay as you go, 30 days kept. The first 5 GB a month is free, which a demo month stays under | $0 |
| Managed Grafana | Essential, at $6 for each user who signs in that month | $0 to $6 |
| Network, identities and budget | The network, security groups, managed identities, Workload ID and Cost Management have no charge | $0 |
| **Total** | | **$19.26 to $25.26** |

### Compute resource group: about $0.98 an hour, only while it runs

| Resource | Size and settings | Per hour |
| --- | --- | --- |
| AKS control plane | Free tier: no uptime SLA, enough for a demo | $0 |
| System node pool | 1 × D2ads_v5: 2 vCPUs, 8 GiB, ephemeral OS disk, so no disk charge | $0.110 |
| User node pool | 1 × D4ads_v5: 4 vCPUs, 16 GiB, ephemeral OS disk. The autoscaler adds a second node only if needed, at $0.22 an hour | $0.220 |
| PostgreSQL compute | Burstable B2s: 2 vCores, 4 GiB. It stops along with the compute | $0.040 |
| App Gateway | WAF v2: the fixed price, plus about 1 capacity unit | $0.482 |
| Load balancers and IPs | 2 Standard load balancers (outbound, and the internal one in front of APISIX), and 2 static IPs | $0.060 |
| Defender for Containers | 6 vCores at $0.0094 each | $0.056 |
| Managed Prometheus | About 10,000 series every 30 seconds, at $0.11 per 10 million samples | $0.015 |
| **Total** | | **$0.983** |

The two disks Qdrant and the APISIX etcd store use (5 and 1 GiB, on Standard SSD) cost under $0.01 an hour. They
are deleted with the cluster, and both are reseeded on start. On Azure, NATS, the PostgreSQL pod and the six
monitoring pods are not deployed.

## What a month costs

| Profile | What runs | Per month |
| --- | --- | --- |
| **Idle** | The data resource group only, between demos | **about $19** |
| **Demo (recommended)** | The compute for about 60 hours a month, such as 15 sessions of 4 hours | **about $84** |
| Always on, gateway for demos | The cluster and database all month, App Gateway for 60 hours, and about 15 GB of logs | about $455 |
| Always on | Everything all month | about $778 |
| Live, later (Phase 15) | Production grade, as below | about $1,800 |

The demo profile is what M4A.6 asks to approve. The $100 budget leaves room for a busy month, and its alerts warn
long before then.

## How to stop paying

These commands come with M4A.6. Each one shows a `what-if` before it changes anything.

| Command | What it does | Then costs | Back in |
| --- | --- | --- | --- |
| `make azure-up` | Creates the compute group, deploys the platform, and seeds the demo data | $0.98 an hour | about 20 minutes |
| `make azure-down` | Deletes the compute group and stops PostgreSQL. Data, images and secrets stay | about $19 a month | `make azure-up` |
| `make azure-destroy` | Deletes both groups, after asking to confirm by typing the name | $0 | rebuilds from scratch |

- **PostgreSQL restarts itself after 7 days stopped.** Azure starts a stopped server again after 7 days. A
  scheduled GitHub Actions job, signed in with OIDC rather than a stored secret, stops it again every day it is
  not needed. Without that job, PostgreSQL would cost $29 a month, and the budget alert would catch it.
- **Key Vault keeps deleted secrets for 90 days.** With purge protection, a deleted vault's name stays reserved for
  that time. `azure-destroy` says so, and the Bicep takes the vault's name as a parameter.

### Budget and alerts

- **The budget:** a Cost Management budget of $100 a month on the subscription, filtered to resources tagged
  `project=windops`.
- **The alerts:** an email at 50%, 80% and 100% of actual spend, and at 100% of forecast spend. The address comes
  from `.env` and is never committed.
- **The tags:** every resource is tagged `project`, `environment` and `owner`, so the cost analysis splits by them.

## Security

On Azure, every in-cluster control stays as it is: mutual TLS and a signed identity between services, Cilium network
policies, non-root read-only pods, and tenant isolation in the database. Azure adds:

| Layer | Controls |
| --- | --- |
| **Coming in** | **App Gateway WAF v2** in Prevention mode, with the Microsoft Default Rule Set 2.1 and the Bot Manager rules. HTTP redirects to HTTPS. TLS ends at the gateway, with its certificate in Key Vault, and starts again to APISIX, which is reachable only on an internal load balancer |
| **Identity** | **Entra Workload ID** for every workload: one managed identity each, federated to its own service account, with roles on only what it uses. There are no connection strings, keys or passwords in the cluster. Kubernetes access is through Entra ID and Azure RBAC, with local accounts disabled |
| **Data** | **PostgreSQL** has private access only, in a delegated subnet with no public address. It accepts Entra sign-in only (password sign-in is off) and TLS 1.2 or later. Each service's identity maps to its own database role. **Key Vault** uses Azure RBAC, purge protection and a firewall that allows only the cluster's subnet |
| **Events and images** | **Service Bus** has local (SAS key) sign-in disabled, requires TLS 1.2, and allows only the cluster's outbound IP. **ACR** has its admin user and anonymous pull disabled, and the cluster pulls with its own identity (`AcrPull`) |
| **Cluster** | The API server accepts only authorized IP ranges. Also on: the Azure Policy add-on with the baseline Pod Security rules, **Defender for Containers**, Image Cleaner, and automatic patch and node image upgrades |
| **Audit** | Activity logs, and the diagnostic logs of AKS, Key Vault, the WAF and PostgreSQL, go to Log Analytics |

### Known gaps, accepted for cost

The live profile closes all three:

- **Private endpoints for Service Bus and ACR.** Both need Premium tiers: Service Bus Premium is about $677 a month.
  Until then, both stay on public endpoints with Entra-only sign-in, and Service Bus also has an IP rule.
- **A private API server.** It needs a jump box or a VPN. Authorized IP ranges stand in for it.
- **Zone redundancy.** A single node in each pool, and a Burstable server with no high availability, cannot span
  zones.

## Reliability

| Concern | Demo profile | Live profile, later |
| --- | --- | --- |
| Cluster | Free tier, 1 node per pool, self-healing nodes | Standard tier with its SLA, 3 system nodes across zones, user pool across zones |
| Database | Burstable, 7 days of point-in-time restore | General Purpose with zone-redundant high availability, 35 days of restore |
| Events | Service Bus Standard: durable, with duplicate detection and dead letters | The same |
| Gateway | App Gateway with autoscaling, 1 instance | At least 2 instances, across zones |
| Rebuild | Everything is Bicep and Kustomize, so `make azure-up` gives the same platform each time | The same, through the delivery pipeline |

The application's own reliability is the same in both: circuit breakers, bounded retries, the transactional outbox,
and a defined failure behavior for every dependency (ADR 0004).

## Efficiency

- **No self-hosted monitoring on Azure.** The Collector sends traces to Application Insights. Container insights
  reads logs, and managed Prometheus scrapes with our own `pods` job. Prometheus, Alertmanager, Grafana, Loki,
  Tempo and Fluent Bit are not deployed (M4A.5).
- **Container insights uses its cost-optimized preset,** which collects logs and events and leaves out the
  performance counters that Prometheus already has. Logs are kept for 30 days, which is free.
- **Ephemeral OS disks.** Nodes start faster, and there is no OS disk to pay for.
- **Autoscaling.** The user pool runs one node and adds a second only under load.
- **One image build.** CI builds each image once and pushes the same digest to ACR.
- **No reservations or savings plans.** They pay off only for resources that run all the time, so they wait for the
  live profile.

## The live profile, for Phase 15

This profile is not part of M4A.6. It shows what changes for production, and it is about $1,800 a month:

- **The cluster:** AKS Standard tier ($73), 3 × D2ads_v5 system nodes ($241), and 3 to 6 × D4ads_v5 user nodes
  (from $482).
- **The database:** PostgreSQL General Purpose D2ds_v5 with zone-redundant high availability (about $308 with its
  storage).
- **The gateway:** App Gateway WAF v2 running all the time, with 2 instances (about $373).
- **Security:** Defender for 18 vCores ($124), and a Key Vault private endpoint ($7).
- **Monitoring:** Managed Grafana Standard ($31, plus $6 per user), and about 1 GB of logs a day ($75).

A 1-year savings plan on the compute would take about a quarter off.

## What is assumed

- **Log volume.** A demo month stays under 5 GB of logs, and always on is about 0.5 GB a day. Application Insights
  samples traces, so this holds if log levels stay at `info`.
- **Prometheus volume.** About 10,000 active series. If the dashboards show more, this is a few dollars a month,
  not tens.
- **Traffic out.** Under the 100 GB a month that is free.
- **Prices change.** The plan records the date it priced. M4A.6 checks them again before asking for approval.
