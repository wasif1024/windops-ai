# ADR 0048: Each tenant turns off its own egress paths

**Status:** Accepted, 2026-11-28, for M13.3b.

## Context

EGR-002 asks that each tenant can disable an egress path, and that the system reports the resulting data gap or
unavailable feature. SRS 9.3 names six paths out of the platform. Only web research had a switch, in its research
policy (ADR 0013). The network policies of ADR 0047 decide which workloads may reach a provider at all, for every
tenant at once. They cannot tell one tenant's data from another's.

## Decision

**The Tenant Service keeps one switch per path, per tenant, on until it is turned off.**

| Path | Goes to | Checked by | While it is off |
| --- | --- | --- | --- |
| `claude` | Anthropic API | Agent Runtime, Investigation | Agents do not investigate. The case says why, and its deterministic checks carry on |
| `embeddings` | Embedding provider | Knowledge | Documents wait to be indexed, and document search pauses, each saying why |
| `langsmith_traces` | LangSmith | Agent Runtime | Runs are not traced. The audit trail is complete either way |
| `langsmith_sandbox` | LangSmith Sandbox | Agent Sandbox Adapter | Agents are told the Sandbox is off for the tenant, carry on, and say so |
| `forecasts` | Forecast provider | Weather | No new forecast is fetched. Kept ones show with their age, and decisions that need a fresh one fail closed |

- Web research keeps its own switch in its research policy, so no path has two.
- **Reading:** anyone in the tenant, and a service acting for it, may read the switches. Each shows where it goes,
  what goes there and what stops while it is off.
- **Changing:** turning one off or on needs `mcp.configure`, which SRS 3.2 gives Platform and Tenant Administrators
  for integrations. It also needs a person, a recent second factor and a reason of at least 10 characters. Each
  change is audited as `tenant.egress.turned_off` or `tenant.egress.turned_on`. The administrator console has a
  "Data egress" tab for it.

**Each service checks the path where it sends, and reports what stops as it reports that path's outage.**
- A switched-off path reuses the outage's behavior from SRS 7.7, with its own reason: "Forecast provider is turned
  off for this tenant (EGR-002)".
- `windops_grpc.egress` reads the switches from the Tenant Service, as the service acting for the tenant.
  - An answer is kept for 30 seconds, so a change takes effect within half a minute.
  - When the Tenant Service cannot answer, the last answer stands.
  - With no answer ever, the path counts as off: nothing leaves for a tenant whose choice is unknown.
- A rehearsal stands in for Claude, so it does not start while Claude is off either. The tenant sees the same thing
  rehearsed or live.
- The Sandbox adapter refuses a switched-off tenant with FAILED_PRECONDITION, not UNAVAILABLE. No caller's breaker
  counts one tenant's choice as the Sandbox failing for every tenant.

## Consequences

- A tenant can keep its case evidence, documents, traces or site coordinates inside the platform, path by path, and
  sees exactly what it loses.
- The Agent Runtime, Investigation, Knowledge, Weather and the Sandbox adapter now call the Tenant Service. The
  network policies regenerated with those calls.
- A switch takes up to 30 seconds to apply. A call already under way finishes.
