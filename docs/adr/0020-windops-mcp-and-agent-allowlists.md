# ADR 0020: WindOps MCP, and each agent's allowlist enforced where its tokens are minted

**Status:** Accepted, 2026-11-19, for M8.6.

## Context

SRS 6.3 gives agents one route to the platform's own records: WindOps MCP. Its example tools are turbine status,
telemetry summary, alarms, history, parts, evidence, impact, policy check and create draft. It is tenant-scoped and
draft only.

SRS 5.3 sets each agent's allowed tools. The rules:
- **MCP-002:** each agent has an explicit allowlist and tenant context.
- **MCP-006:** an agent that reads external content has no write tools. Only schema-validated fields, with source
  references, pass to an agent that does.
- **MCP-007:** instructions in retrieved content cannot change a plan, a permission or an allowlist.

[ADR 0019](0019-agent-identities-delegations-and-agent-tokens.md) gave each agent its own 120-second tokens, minted by
the Identity Service.

## Decision

**WindOps MCP is a server of its own, `windops-mcp-windops`.** Like every server, it is built on the shared
`windops-mcp` library, so ADR 0014's controls hold for each call. Its tools all read:

| Tool | Service | Notes |
| --- | --- | --- |
| `get_turbine` | Asset | Model, rating and status. No serial number |
| `get_latest_telemetry` | Telemetry | Each metric's latest reading and quality label |
| `list_alarms` | Alarm | No one's user ID: who acknowledged or shelved an alarm is left out |
| `get_case`, `list_cases` | Investigation | A case, and a turbine's cases as its fault history |
| `get_evidence` | Investigation | A frozen snapshot's items, each item's content clipped at 4,000 characters |
| `estimate_impact` | Production Impact | The service's deterministic estimate, which an agent explains, never changes |
| `check_policy` | Policy | The deterministic rules' outcome: `pass`, `escalate` or `block` |

- **Tenant:** every tool calls its service as the verified caller, and the tenant comes from the token. No tool
  takes a tenant, and an input naming one is refused.
- **Permissions:** each service decides what the caller may read, and an agent may only read and draft (SEC-009).
- **Outputs:** they are named field by field, so a field a service adds later reaches no agent until it is listed.
- **Recorded results:** `estimate_impact` and `check_policy` keep the result they compute, as their services always
  do, so a later step can cite it. A retry by the same caller and agent is the same request, and gets the same
  result.
- **Later:** parts and `create_draft` arrive with the Inventory and Maintenance services (Phase 9).

**Each agent's allowlist lives in the Identity Service, where its tokens are minted** (`windops_identity.targets`).

| Agent | Tools (SRS 5.3) |
| --- | --- |
| Supervisor | `get_case`, `list_cases`, `get_evidence`, `check_policy` |
| SCADA Analyst | `get_turbine`, `get_latest_telemetry`, `list_alarms`, `get_evidence` |
| Reliability Engineer | Documentation's `search_documents` and `read_passage`; `get_turbine`, `list_cases`, `get_evidence` |
| Production Impact | `get_case`, `get_turbine`, `estimate_impact` |
| Maintenance Planner | `get_case`, `list_cases`, `get_turbine`. No web content, ever |
| Safety and Procedure Researcher | Documentation's two tools, Tavily's three, and Weather's `get_forecast`. Read-only |

- **Refused:** a token for a tool off the list is refused, and the refusal is logged.
- **Fixed in code:** nothing an agent reads, whatever it says, can widen a list (MCP-007).
- **Narrow tokens:** a token names only the tools asked for, so it calls nothing else.
- **Unchanged:** the user's own permissions still decide what each call returns.
- **Never an agent's:** the development servers, Filesystem and PostgreSQL.

**External content and writing are kept apart (MCP-006), and checked twice:**
- **Classified:** the Identity Service classes each tool as returning external content (Documentation, Tavily,
  Filesystem's `read_file`) or as writing, which none does yet.
- **At startup:** when its targets load, it refuses to start if any agent holds both kinds, an unknown tool, or a
  development server's tool.
- **In the tests:** a repository test checks that classification against every server's own tool definitions, so a
  server that starts returning external content, or writing, cannot be left unclassed.

**The other half of MCP-006** concerns what passes from a reader to a writer:
- **Now:** only the Maintenance Planner will write, and it reads no external content.
- **Later:** what it receives from the readers will be the structured, cited results of M8.8. These are
  schema-validated fields with source references, never raw text.

## Consequences

- **Proved on the cluster:** `make agent-tools-check` uses a real login and the real services. It proves:
  - every agent gets each tool on its list, 25 in all, and is refused the WindOps tools and web search off it;
  - WindOps MCP answers each agent from the real services, in the user's tenant only;
  - a token calls only its own tools;
  - every call is audited as the agent, on the user's behalf.

  It opens a case of its own on the demo turbine, and cancels it at the end.
- **A new server needs:**
  - its own Keycloak client, Redis login and NATS login, from `make identity-seed` and `make secrets`;
  - NATS and Redis restarted, to load the new logins.
- **What changes with M8.7:** an agent's definition names its tools, and a test will keep each definition matched
  to its allowlist here.
