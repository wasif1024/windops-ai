# ADR 0014: MCP tools behind per-call tool tokens

**Status:** Accepted, 2026-11-18, for M7.5.

## Context

Agents reach the platform only through MCP servers (SRS 6.3), which call services over gRPC and cannot bypass their
controls. The SRS requires that:
- clients are authenticated, with token audience and scope checked on each call (MCP-001);
- tool inputs and outputs are schema-validated, with timeouts, quotas, rate limits and audit logs (MCP-003);
- agents never hold long-lived secrets or broad bearer tokens (MCP-004);
- retrieved web pages and documents are wrapped and labelled as untrusted before use (MCP-005);
- tokens are audience-bound, scoped per tool and short-lived (SEC-008).

M3.7 already exchanges a user's Keycloak token, through the Identity Service, for a 120-second token naming one
service. The SRS leaves open which MCP SDK to build on, what a tool's scope is, and how untrusted text is marked.

## Decision

**One shared library, `windops-mcp`,** which every MCP server is built on. A server only names its tools. The
library applies every control to every call, so no server can leave one out.

**The SDK: the official MCP Python SDK, on its 1.x line (1.30.0).**
- **Why not 2.x:** LangChain's MCP adapters, which the Phase 8 agents use, require `mcp<2`.
- **Moving up:** to 2.x when the adapters support it.
- **Transport:** streamable HTTP, without sessions. Each HTTP request is a tool call, checked on its own.
- **Validation:** the SDK's own input validation is turned off, because each tool's model validates instead.

**Tool tokens (MCP-001, MCP-004, SEC-008):**
- **What a token is:** a Keycloak token exchanged for one MCP server, such as `windops-mcp-documentation`. Its
  audience names that server alone.
- **Per-tool scopes:** each tool it may call is named as a scope, `mcp.<server>.<tool>`.
- **Where scopes are defined:** in the Identity Service's own list, `windops_identity.targets`. Keycloak's client
  scopes are seeded from that list, so an unlisted scope cannot be issued.
- **What the Identity Service refuses:** an exchange with no tool, or with a scope from another server.
- **Lifetime:** a token lasts 120 seconds.
- **Checked on every call:**
  - Keycloak's signature and the issuer;
  - the audience is exactly this server, so a user's own login is refused, being for `windops-api`;
  - the lifetime is at most five minutes, whatever the token's expiry says;
  - the token names one tenant and one user;
  - it carries the tool's scope.
- **What the token reaches:** no handler, log or result.
- **Calling services:** a handler calls them as its server, by certificate. It carries the user's verified context,
  signed for no longer than their token has left. Services then decide what that user may see.

**Each tool's definition is checked at startup (MCP-003):**
- Its input refuses unknown fields, and every string, list and map in it has a size limit.
- Its output has a schema.
- It has a timeout of at most 60 seconds, a rate per caller each minute, and a quota per tenant each day.
- A tool returning external content must return it as `Untrusted`, and may only read.
- A definition that breaks a rule stops the server from starting.

**For each call, in order:**
1. **Scope:** an unknown tool and one the token may not call give the same denial, so tool names cannot be probed.
2. **Input:** the arguments are checked against the input schema. A refusal names the wrong fields, never their
   values.
3. **Limits:** they are counted in Redis, under the server's own key prefix and the caller's tenant. The user's ID
   goes into a key only as a hash. If Redis is down, nothing is let through.
4. **Handler:** it runs within its timeout. A failure reaches the caller only as an outcome: `refused` (with the
   handler's own safe message), `unavailable`, `timed_out` or `failed`.
5. **Output:** the result is checked against the output schema, so a stray field fails the call rather than leaking.
6. **Audit:** the call goes to the Audit Service as `mcp.<server>.<tool>`, with its outcome, never its arguments or
   result.
   - **An agent's own identity:** when it is in the token (RFC 8693's `act` claim, from M8.5), the agent is the actor,
     on the user's behalf (SEC-010).
   - **If the record cannot be sent:** a result is withheld.

A tool list shows only the tools the token may call.

**Untrusted content (MCP-005, MCP-007):** external text is wrapped in an envelope with:
- **trust:** `untrusted`;
- **kind and source:** what the text is, and its citation or address;
- **label:** it is information only, and cannot change instructions, plan, permissions or tools;
- **flags:** the screening library adds `instructions_to_ai`;
- **truncated:** whether the text was cut.

Inside the envelope:
- **The text:** sits between markers holding a random nonce, so it cannot close the envelope and speak as the
  platform. A marker spelled inside the text is defused.
- **Taken out:** control characters, and the Unicode controls that reorder or hide text.

## Consequences

- **Short tokens:** a token is good for 120 seconds, so the Agent Runtime exchanges a fresh one for each burst of
  tool calls. No long-lived credential reaches an agent.
- **Adding a tool:** its scope is added in the Identity Service's list, and `make identity-seed` creates it in
  Keycloak.
- **Proof:**
  - `make mcp-check` proves this with a real login, real exchanges and Keycloak's real keys, against a probe server
    built with the library.
  - The probe server keeps its limits and audit records in memory.
  - M7.6 deploys the first servers, each with its own Redis and NATS users. `make mcp-servers-check` proves
    Redis-backed limits and the audit stream on the cluster ([MCP integrations](../mcp-integrations.md)).
- **Audit without an outbox:** an MCP server changes nothing, so it has no state for a record to commit with
  (EVT-002). Each record is published straight to JetStream, on the server's own subject, and acknowledged before
  the result is returned. That keeps it durable (EVT-004). NATS lets each server publish only that subject.
- **Mutual TLS:** a server's MCP endpoint is served only over HTTPS, to clients presenting a certificate from the
  cluster's CA, so a token alone, taken out of the cluster, reaches nothing. Plain HTTP carries only health and
  metrics.
- **Deferred to M8.6:** per-agent allowlists, and keeping write tools from agents that read external content.
  Here, a tool returning external content can only read, and says so in its annotations.
