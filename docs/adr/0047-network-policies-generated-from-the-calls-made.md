# ADR 0047: Network policies, generated from the calls each workload makes

**Status:** Accepted, 2026-11-28, for M13.3.

## Context

K8S-001 asks for a CNI that enforces NetworkPolicy, with default-deny ingress and egress in every namespace. EGR-001
asks that egress outside the cluster reach only named endpoints. Until now there was no policy at all: any pod could
reach any pod, and anywhere on the internet. Mutual TLS and signed identities refused a call they did not expect,
but nothing stopped the connection.

About 40 workloads make about 200 calls, and the calls change as services do. Policies written by hand would drift
from the code, and a missing rule shows only as a timeout at run time.

## Decision

**Every namespace denies all ingress and egress.**
- Each of the five namespaces in SRS 11.2 has a Kubernetes NetworkPolicy that selects every pod and allows nothing.
  A pod nobody wrote a rule for, such as a check's client, reaches nothing and is reached by nothing.
- Allow rules are Cilium policies, since Cilium enforces policy locally and on AKS (ADR 0006). Cilium can name the
  Kubernetes API, the node and DNS names, which a Kubernetes NetworkPolicy cannot.

**Each workload is allowed only the calls it makes, generated from what it reads.**
- `make network-policies` writes `infrastructure/kubernetes/base/network`, one policy per workload. A test fails
  when the files are out of date. The calls come from:
  - each `WINDOPS_..._ADDRESS`, `_URL` or `_ENDPOINT` setting the workload's code reads, or a shared library it uses
    reads, resolved through `.env.example`;
  - each in-cluster address in its manifests, its ConfigMaps or its own code, such as the recordings at the
    Evaluation Service;
  - the data stores its Secrets name: `-database` is PostgreSQL, `-events` NATS, `-cache` Redis and `-keycloak`
    Keycloak;
  - the gateway's routes and plugin settings, which live in etcd;
  - a short declared list, each entry with its reason: the Agent Runtime's MCP servers, whose addresses it builds,
    and fluent-bit's Loki, whose host and port are on separate lines.
- Each call is allowed at both ends: out of the caller and into the callee, on the pod's own port. Cilium decides
  after a Service has picked the pod.
- Migration Jobs carry `app.kubernetes.io/component: migration` and reach only PostgreSQL.

**Outside the cluster, only the named endpoints of SRS 9.3, by DNS name.**

| Workload | Endpoint | For |
| --- | --- | --- |
| Agent Runtime | `api.anthropic.com`, `api.smith.langchain.com` | Claude, and redacted traces |
| Agent Sandbox Adapter | `api.smith.langchain.com`, `*.langchain.com` | LangSmith Sandbox |
| External Intelligence | `api.tavily.com` | Web research |
| Knowledge | `api.openai.com` | Embeddings |
| Weather | `api.open-meteo.com` | Forecasts |

- Every pod looks names up through Cilium's DNS proxy, which learns each name's addresses, so a rule by name can
  allow them. No policy allows an address range or the `world` entity.
- A sandbox's data plane is an address LangSmith's API returns when the sandbox is made, so only a pattern can name
  it. It is checked when the sandbox next runs live, in the paid session after the last phase.

**What else is allowed.**
- **Into the gateway, from anywhere:** it is the platform's only way in (SRS 9.5).
- **Prometheus:** every pod's metrics port, the Kubernetes API to find them and read container figures, and Cilium's
  operator, which runs on the node's own network.
- **The Kubernetes API:** for Prometheus, kube-state-metrics and fluent-bit only. No service of ours mounts an API
  token.
- **Not affected:** the kubelet's probes and `kubectl port-forward`, which the checks use, do not cross a pod's
  network policy.

**Not on Azure yet.**
- The `azure` overlay has no network policies. There, the data stores and observability are managed services
  outside the cluster, so the endpoints differ. On AKS, rules by DNS name also need Advanced Container Networking
  Services. The Azure stage adds both.

**Tenant egress switches move to M13.3b.**
- EGR-002 needs a switch per tenant for each egress path, and a stated data gap when it is off. Only web research
  has one today (ADR 0013). The other paths get theirs in M13.3b.

## Consequences

- A compromised or misbehaving pod reaches only what its workload already calls. Nothing in the cluster reaches the
  internet except the five workloads above, and only their named endpoints.
- A new call needs `make network-policies`, and the test says so. A call the generator cannot see goes in the
  declared list, with its reason.
- Writing the generator found four service addresses missing from `.env.example`: Approval, Inventory, Maintenance
  and Notification. A fresh `.env` would have left them unset. They are added, and a test checks every address the
  code reads has a default.
