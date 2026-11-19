# ADR 0021: The agents and skills, on restricted Deep Agents

**Status:** Accepted, 2026-11-19, for M8.7.

## Context

SRS 5.3 names six agents: the Supervisor, the SCADA Analyst, the Reliability Engineer, Production Impact, the
Maintenance Planner, and the Safety and Procedure Researcher. SRS 11.5 names six skills, and puts both in the
repository's `agents/` and `skills/` folders. SRS 5.2 names Deep Agents as their harness.

[ADR 0015](0015-agent-stack-vendor-checks.md) found that Deep Agents needs four restrictions:
- a model named for every agent;
- no default file or shell tools;
- no summarization middleware;
- no general-purpose subagent.

The earlier ADRs set the rest:
- [ADR 0017](0017-the-claude-policy-as-versioned-configuration.md) put every call to Claude under the policy.
- [ADR 0018](0018-the-investigation-graph-and-its-checkpoints.md) set the graph.
- [ADR 0019](0019-agent-identities-delegations-and-agent-tokens.md) gave agents their own tokens.
- [ADR 0020](0020-windops-mcp-and-agent-allowlists.md) gave each agent its allowlist.

## Decision

**Each agent is a folder,** `agents/<name>/`:
- **`agent.yaml`:** its version, its role in the Claude policy (which alone sets its model and effort, LLM-001), its
  tools, its skills, and, for the Supervisor, whom it may delegate to.
- **`prompt.md`:** its stable system prompt.

**Each skill is `skills/<name>/SKILL.md`,** with its name, version and description in its frontmatter.

**Checks on loading:**
- **Role:** every agent's role is in the Claude policy.
- **Prompts:** each holds nothing that changes between runs, so it caches (LLM-007).
- **References:** every skill and delegate an agent names exists.
- **Tools:** a repository test checks that each agent's tools are exactly its allowlist in the Identity Service, so a
  definition and its tokens can't drift apart.

**Pinned (AGT-004):** each prompt, tool list and skill has a content hash, and every investigation run pins them all.
A run resumed after any of them changed ends as `stale`.

**Every agent's model is `MeteredChat`, a LangChain chat model whose calls all go through the Claude policy's
client.** Each call goes through, in order:
1. the run's budget;
2. the role's model and effort, with refusal fallback;
3. the call recorded, with its tokens and cost;
4. the stop reason, checked before anything is read.

Tool bindings pass through. Forced tool choice is refused (LLM-004).

**Deep Agents is restricted by a harness profile,** registered for this platform's models (provider `windops`):
- **No summarization:** the middleware that rewrites earlier history is excluded (LLM-008).
- **No general-purpose subagent:** the Supervisor delegates only to the five specialists its definition names,
  through Deep Agents' `task` tool. Specialists can't delegate.
- **No file or shell tools:**
  - **Excluded:** writing, editing, deleting, executing, to-dos, glob and grep.
  - **What stays:** listing and reading, which reach only the run's own skills. These are held in the agent's
    in-memory state, never the host filesystem. A permission rule denies everything else.
  - **Per agent:** each specialist sees only its own skills.

**Tools are the MCP servers', called with the agent's own tokens.**
- **Per call:** each tool asks the Identity Service for a 120-second token for that agent and that one tool, and
  calls the server over mutual TLS with the runtime's certificate.
- **Where the token goes:** only into the Authorization header. Never into a prompt, a tool result or state
  (SEC-008).
- **Schemas:** a tool's input schema is the server's own, read once per process.
- **Untrusted text:** text the server wrapped as untrusted reaches the agent still wrapped (MCP-005).
- **Budget:** every agent's tool calls count against the run's budget, 50 by default (SRS 5.6). That limit is now
  in the Claude policy, as version 2.

**In the graph:**
- **Step 5 runs the team.** Steps 4 to 9 happen inside it: the Supervisor reads the case, plans, delegates and
  synthesizes.
- **Steps 6 to 8 and 11** then record, from the tools actually called, whether each happened, and name a gap where
  it didn't.
- **The report:** the Supervisor's report is text until M8.8 gives it its structure.
- **Pins:** the graph is now version 2.

**A run can rehearse the team at no cost.** It assembles the real team, with real tools, tokens and MCP servers, and
swaps only the model for a script. Each agent then makes one real, free tool call from its own allowlist. No call
reaches Claude, and no web search is made.

**The first run with Claude itself moves to the M8.13 exit gate.** The person responsible chose on 2026-11-19 not to
pay for model calls at every step. Everything a model doesn't decide is proved without one:
- by scripted unit tests;
- by `make team-check`, which rehearses on the cluster.

## Consequences

- **Proved for free:** `make team-check` rehearses on the cluster. It proves that:
  - the Supervisor delegates to all five specialists through Deep Agents;
  - each agent calls a real tool from its own allowlist, with its own token, over mutual TLS;
  - every call is audited as that agent, on the user's behalf;
  - the run is pinned to every prompt, tool list and skill;
  - no token is kept in the run's checkpoints;
  - nothing is asked of Claude.
- **Not yet proved:** how well the prompts work. That is what the real run at M8.13 and the evaluation gates (Phase
  12) are for, and a prompt changes only with a new version.
- **Restarts:** the team runs within one graph node. A worker that stops mid-investigation runs the team again on
  its next attempt, and pays for its calls twice, both recorded against the budget (ADR 0018).
- **What M8.8 adds:**
  - a structured, cited result;
  - at most two repairs;
  - a policy check of every recommendation;
  - FR-IMP-004's check that every figure the Production Impact agent gives is the estimate's own.
- **Image size:** the runtime's image carries `agents/` and `skills/`, and its memory limit is raised to 640 MiB
  for Deep Agents.
