# Phase 9 exit gate

**Result:** passed on 2026-11-20, on the local Minikube cluster, at no cost. Every agent run was rehearsed, or a dry
run against the stand-in Claude API (ADR 0027).

SRS section 12 sets the gate: *an agent can create a draft, and all attempts to submit or reserve as an agent are
denied.*

```bash
make agent-denial-check   # the gate itself, about a minute
make phase-9-gate         # the gate, then every Phase 9 check and the earlier ones Phase 9 touched, about 30 minutes
```

## The gate itself

`make agent-denial-check` has an agent draft the way SRS 5.4 step 12 does: a fault-triggered investigation, for the
tenant's fault-investigator, with the real team, tools and tokens (ADR 0031). It then tries every layer an agent
could reach for a submission or a reservation:

```text
ok - No MCP server offers, and no agent holds, a tool that submits, approves, withdraws, reserves, releases or changes stock
ok - Authorization holds every agent to reading and maintenance.draft, whoever it acts for
ok - rehearsal: the planner advised in step 5 without drafting, then in step 12 read the history, found parts, and drafted once
ok - rehearsal: the Maintenance Service holds it as a draft by the planner agent for the fault-investigator, never submitted
ok - An agent created a draft: wo_373aa8bf9aa5780c44b6, in step 12 (M9.3)
ok - Identity mints no token for a submitting tool (INVALID_ARGUMENT), nor for the draft tool to an agent whose allowlist lacks it (PERMISSION_DENIED)
ok - WindOps MCP refuses a token for find_parts on the draft tool, and has no submit tool
ok - The planner may submit and reserve
ok - Authorization lets the planner's agent draft, and refuses it maintenance.submit, maintenance.approve, maintenance.complete, inventory.reserve, inventory.update
ok - Authorization lets the fault-investigator's agent draft, and refuses it maintenance.submit, maintenance.approve, maintenance.complete, inventory.reserve, inventory.update
ok - The planner, a person, reserves a part for the agent's draft
ok - As the planner's agent, every attempt is refused: submit the draft, withdraw it, reserve parts for it, release the planner's reservation, adjust stock, change the part
ok - As the fault-investigator's agent, every attempt is refused: (the same six)
ok - As the fault-investigator alone, every attempt is refused: (the same six)
ok - Nothing changed: the draft is still a draft, the stock is as the planner left it, and the one reservation is the planner's
ok - Every refusal reached the audit trail as authorization.denied, the agent as the actor on its principal's behalf (SEC-010): 16 distinct
```

The controls hold at five layers:
- **The agents' allowlists:** no agent holds a tool that submits, approves, withdraws, reserves, releases or changes
  stock, and no MCP server offers one.
- **The Identity Service:** it mints no token for such a tool, nor for a real tool outside the agent's own allowlist.
- **WindOps MCP:** a token for one tool calls no other.
- **The Authorization Service:** it holds every agent to reading and drafting, whatever the person behind it may do.
- **The services themselves:** the Maintenance and Inventory services refuse an agent, or an identity with no person,
  before they ask Authorization. So no change to the permission table could let an agent submit or reserve.

## Found while proving it

- **Unaudited refusals.** When the Maintenance and Inventory services refused an agent themselves, nothing was
  recorded, because Authorization was never asked. Both now audit the refusal as `authorization.denied`, naming the
  permission, with the agent as the actor on its principal's behalf. The Denials page shows these with
  Authorization's own (ADRs 0029 and 0030, amended).
- **A cross-tenant check that had fallen behind.** `make cross-tenant-check` attacked 4 services' databases, those
  of Phase 3. It now attacks all 17 that store data, 116 tables, and a repository test holds its list to the services
  with migrations. Every table passed. Its audit isolation step was also made safe against records that arrive while
  it reads.
- **A step missed when deploying M9.2.** Keycloak's client scopes are seeded from the Identity Service's tool list, and
  `find_parts` and `check_parts` had not been seeded. So `make mcp-servers-check` failed: Keycloak refused a user's
  token exchange that named them. Agents were never affected, as the Identity Service mints their tokens itself. After
  `make identity-seed`, the check passed. **A new MCP tool needs `make identity-seed` when it is deployed.**

## Every check

`make phase-9-gate` ran 18 checks in 26 minutes. `mcp-servers-check` failed for the Keycloak seed above, and passed
when run again after it.

| Check | Seconds | What it proves for the gate |
| --- | --- | --- |
| `agent-denial-check` | 54 | An agent drafts, and every attempt to submit or reserve as an agent is denied and audited |
| `maintenance-check` | 42 | Work orders validated, drafted by anyone, submitted by a person |
| `inventory-check` | 49 | Parts, compatibility, stock and reservations, by a person alone |
| `planner-check` | 42 | The planner's proposal and one draft in step 12, rehearsed and as a dry run |
| `frontend-check` | 278 | Every console journey, the planner screens included, at phone size too |
| `team-check` | 30 | The team rehearsed: an operator's planner is refused its draft |
| `regression` | 328 | 12 of 12 scenarios, rehearsed, over the new graph |
| `phase-8-gate MODE=stand_in` | 208 | The Phase 8 gate's dry run, now through step 12 |
| `progress-check` | 20 | Live progress through the gateway, to pending review |
| `agent-tools-check` | 33 | WindOps MCP's tools, with real agent tokens |
| `agent-identity-check` | 4 | Delegations, agent tokens; agents only read and draft |
| `token-exchange-check` | 1 | Tool scopes issued only for their owner |
| `mcp-servers-check` | rerun | The MCP servers over mutual TLS, limited in Redis, and every one of 71 calls audited |
| `authorization-check` | 100 | Roles, scopes, service grants and audited denials |
| `cross-tenant-check` | 83 | Every service's database and API attacked across tenants: 116 tables, 20 API attempts |
| `audit-check` | 121 | A tamper-evident audit record |
| `nats-check` | 51 | JetStream keeps events and limits each publisher |
| `migrate-check` | 112 | Migration Jobs run once per release |

**Left out:**
- **They cost money or compute, and Phase 9 did not change them:** `make workflow-check`, which asks Claude for one
  plan (it passed for M9.3, at $0.0033); `make claude-check`; and `make sandbox-check`, which uses LangSmith compute.
- **The unit suite:** `make check`, 2,475 tests, passed before the gate.

## Not proven here

- **A draft by Claude itself.** On the stand-in, the planner's turns are scripted. Claude's own drafting needs a paid
  investigation. The account has about $3.59 of credit, less than Phase 8's one investigation cost ($4.09), so it
  waits for the person responsible to decide.
- **Approval, and reservation by a workflow:** both come with Phase 10.
