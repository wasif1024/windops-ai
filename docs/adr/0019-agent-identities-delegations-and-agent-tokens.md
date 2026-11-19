# ADR 0019: Agent identities: delegations, and agent tokens from the Identity Service

**Status:** Accepted, 2026-11-19, for M8.5.

## Context

SRS 9.2 sets who an agent acts as:
- **A user-started investigation:** a short-lived token exchanged on behalf of the user and the Agent Runtime, with
  the intersection of the user's permissions and the agent's allowlist.
- **A fault-triggered investigation:** the tenant-scoped service principal `fault-investigator`, which may read
  evidence and create drafts, nothing more.

The security requirements:
- **SEC-008:** tokens are audience-bound, scoped per tool, short-lived, and never in prompts or tool results.
- **SEC-009:** agent and service identities are structurally ineligible for human approval.
- **SEC-010:** audit records name both the acting agent and the principal behind it.

[ADR 0014](0014-mcp-tools-behind-per-call-tool-tokens.md) gave MCP servers per-call tool tokens: Keycloak tokens
that the Identity Service exchanges for one server and its tools, lasting 120 seconds. That serves a person calling
a tool. Two things are missing for agents:
- **Time:** a run lasts minutes, longer than the user's own token. Keycloak won't issue a token for a user without a
  current token to exchange, and a fault-triggered run never had one.
- **The agent:** Keycloak's standard token exchange doesn't add RFC 8693's `act` claim, so a token can't name the
  agent.

The person responsible chose on 2026-11-19, from three options, to have the Identity Service mint agent tokens
itself. The others were rejected:
- **Keycloak refresh tokens, stored by the platform:** these are long-lived user credentials, and they still can't
  name the agent.
- **A Keycloak service account per agent:** the user would be lost, so the intersection of permissions can't hold.

## Decision

**A delegation lets a run's agents act for one principal, in one tenant, for a bounded time.** The Identity
Service keeps them in a tenant-owned table.

| Principal | Started by | Proof |
| --- | --- | --- |
| `user:<Keycloak ID>` | Web BFF or Investigation, by certificate | The user's own current Keycloak token, introspected at Keycloak, then dropped: never stored or logged. The caller's identity must be that user |
| `service:fault-investigator` | Investigation only, by certificate | The Investigation Service acting as itself. A user cannot delegate as it |

- **Lifetime:** a delegation lasts an hour by default and four hours at most. It can be ended early, and ending it
  twice changes nothing.
- **Linked to the run:** the Agent Runtime records the delegation on the run, and ends it when the run ends. If the
  runtime can't reach the Identity Service then, the delegation still expires.
- **Audit:** starting and ending one are audited, naming its principal.

**An agent token is minted for each burst of tool calls,** only for the Agent Runtime, by its certificate.
- **Issuer:** the Identity Service signs it with its own ES256 key, as `windops-identity-agents`.
- **Audience:** one MCP server.
- **Scope:** only that server's own tools, as asked.
- **Subject:** the principal: the user's Keycloak ID, or `service:fault-investigator`.
- **`act`:** `{"sub": "agent:<name>"}`, for one of the six agents of SRS 5.3.
- **Lifetime:** 120 seconds, the same as an exchanged user token.

Before each user token, Keycloak is asked again whether the user is still enabled and still in the tenant. The
answer is remembered for a minute. A user disabled or removed from the tenant delegates nothing more.

**The signing key:**
- **The key:** a P-256 key that `make secrets` creates.
- **Where it lives:** the Secret `windops-agent-token-key`, which only the Identity Service's pod mounts.
- **Without it:** the service runs, and no agent token can be minted.
- **The public key:** published by `GetAgentTokenKeys`. MCP servers fetch it over mutual TLS, so nothing in between
  can swap it, which a JWKS over plain HTTP inside the cluster wouldn't guarantee.

**MCP servers trust two issuers, each for one kind of caller:**
- **Keycloak**, with RS256 keys over verified HTTPS, for a person's own exchanged token. A Keycloak token that names
  an agent is refused: only the Identity Service names agents.
- **The Identity Service**, with ES256 keys over mutual TLS, for agents. A token from it that names no agent is
  refused.

Every other check from ADR 0014 holds for both: one audience, at most five minutes, one tenant, and tool scopes.

**Services see the agent:**
- **The forwarded identity:** the identity an MCP server signs for its calls now carries an `actor`.
- **`fault-investigator`:** it is forwarded as a service, with no user, so the Authorization Service decides from
  its fixed grants.
- **Audit:** an MCP server audits each tool call as the agent, on the principal's behalf (SEC-010), as ADR 0014
  provided for.

**Agents may only read and draft (SEC-009), structurally:**
- **In the Authorization Service:** an identity with an actor may use only `*.read` permissions and
  `maintenance.draft`, and only in its own tenant, whatever its principal's roles. No grant or tenant setting can
  widen that. Submitting, reserving, approving, reviewing, managing and configuring are refused before the
  principal's grants are looked at.
- **Audit:** a refusal is audited as the agent, on the principal's behalf.
- **Listing:** `ListMyPermissions` for an agent shows only what it may use.
- **`fault-investigator`'s grants:** reads of turbines, telemetry, alarms, investigations, knowledge, weather,
  policy and inventory, plus `maintenance.draft`. It is the one principal without a person behind it that may make
  a change, and only a draft.

**Where tokens go:** the runtime gets agent tokens through each run's context, under the run's own delegation and
tenant. A token goes only into a tool call's Authorization header. It is never put in graph state, a checkpoint, a
prompt, a tool result or a log (SEC-008).

## Consequences

- **Proved on the cluster:** `make agent-identity-check` uses a real login and the services' own certificates. It
  proves:
  - delegations for a user and for `fault-investigator`;
  - 120-second agent tokens answered by the real Documentation MCP server;
  - audit records naming the agent and the principal;
  - the real Authorization Service holding an agent to reading and drafting;
  - every call refused for the wrong caller, the wrong tenant or an ended delegation.
- **Allowlists:** what each agent may ask for, its allowlist, comes in M8.6. Until then any of the six agents may
  get a token for any MCP server's own tools. The user's own permissions still decide what each call returns.
- **Revocation:** a token already minted stays valid for up to 120 seconds after its delegation ends. That is the
  same bound as every exchanged token.
- **Not yet wired:** Investigation starts delegations, and passes them to the runtime, in M8.11.
- **Key rotation:** the published key set can hold several keys. Rotating the signing key is a runbook in Phase 14.
- **Azure:** the signing key is not yet in Key Vault, so agent tokens are local only until Phase 15 adds it, as for
  the provider keys.
