# MCP integrations

Agents reach the platform only through MCP servers (SRS 6.3). Each server is a separate workload in the
`windops-mcp` namespace. It calls domain services over gRPC as the verified user, so it can never do more than
that user could. Every server is built on the shared library `windops-mcp`, which applies the same controls to
every call ([ADR 0014](adr/0014-mcp-tools-behind-per-call-tool-tokens.md)).

## Servers

| Server | Workload | Calls | Access boundary | Runs |
| --- | --- | --- | --- | --- |
| Documentation MCP | `windops-mcp-documentation` | Knowledge Service | Document permissions, checked by the service before any text is returned | Everywhere |
| Tavily MCP | `windops-mcp-tavily` | External Intelligence Service | Generic web research only. It holds no Tavily key | Everywhere |
| Weather MCP | `windops-mcp-weather` | Weather Service | Weather only, needing `weather.read` | Everywhere |
| WindOps MCP | `windops-mcp-windops` | Asset, Telemetry, Alarm, Investigation, Production Impact and Policy services | The platform's own records, tenant-scoped, read only; each service checks the caller ([ADR 0020](adr/0020-windops-mcp-and-agent-allowlists.md)) | Everywhere |
| Filesystem MCP | `windops-mcp-filesystem` | Its own read-only sandboxes | Synthetic documents and evaluation fixtures, needing `knowledge.read` | Locally only |
| PostgreSQL MCP | `windops-mcp-postgres` | PostgreSQL statistics views | Read-only diagnostics, needing `evaluation.manage` | Locally only |

WindOps MCP, with tenant-scoped tools and draft-only writes, comes in M8.6.

**Filesystem and PostgreSQL are for development only.** Only the local overlay deploys them, through
`infrastructure/kubernetes/base/development`, and never Azure. A test holds this.

## Tools

Every tool has a timeout and two limits: a rate for each caller each minute, and a quota for each tenant each day.

| Tool | What it does | External text | Timeout | Per caller each minute | Per tenant each day |
| --- | --- | --- | --- | --- | --- |
| `documentation.search_documents` | Finds passages for a query. Filters are a wind farm, applies-to, types and a limit of 1 to 20, and replaced revisions can be included | Passage text | 10s | 30 | 2,000 |
| `documentation.read_passage` | Reads one passage by its citation, `kn:<document>@<revision>#<passage>` | Passage text | 5s | 60 | 4,000 |
| `tavily.search_web` | Researches one topic with a generic query, for a wind farm or turbine if given | Every source's title and text | 30s | 10 | 200 |
| `tavily.extract_page` | Reads up to 5 sources of a past search in full | Every page | 30s | 10 | 200 |
| `tavily.get_research` | Reads a past search as it was recorded | Every source and page | 5s | 30 | 2,000 |
| `weather.get_forecast` | Gives a turbine's hourly forecast over a window of up to 7 days | None | 15s | 30 | 2,000 |
| `windops.get_turbine` | Gives a turbine's wind farm, model, rating and status | None | 5s | 60 | 2,000 |
| `windops.get_latest_telemetry` | Gives a turbine's latest reading of each metric, with its unit and quality label | None | 5s | 60 | 2,000 |
| `windops.get_telemetry_summary` | Summarizes a frozen telemetry snapshot per metric: counts by quality, range, mean, first and last, trend per hour and slices, each citable | None | 20s | 20 | 2,000 |
| `windops.list_alarms` | Lists a turbine's alarms, newest first, with code, severity and state | None | 5s | 60 | 2,000 |
| `windops.get_case` | Gives a case, with its current evidence snapshot | None | 5s | 60 | 2,000 |
| `windops.list_cases` | Lists a turbine's cases: its fault history | None | 5s | 30 | 2,000 |
| `windops.get_evidence` | Gives a frozen evidence snapshot's items, each item's content clipped at 4,000 characters | None | 10s | 30 | 2,000 |
| `windops.estimate_impact` | Gets the Production Impact Service's deterministic estimate for an action on a case | None | 20s | 10 | 500 |
| `windops.check_policy` | Gets the Policy Service's deterministic outcome on a proposed action: pass, escalate or block | None | 20s | 10 | 500 |
| `windops.find_parts` | Finds spare parts that fit a turbine's model, with how many are available in its wind farm's store. Holds nothing | None | 10s | 30 | 2,000 |
| `windops.check_parts` | Checks whether named parts fit a turbine and are available in its wind farm's store: available, short, incompatible, retired or unknown. Holds nothing | None | 10s | 30 | 2,000 |
| `windops.create_draft_work_order` | Drafts a work order for a case, validated at once, returning its problems. The one write tool any agent holds: only the Maintenance Planner's, as it reads no external content | Draft only | 20s | 10 | 200 |
| `filesystem.list_files` | Lists a folder in `documents` or `evaluations` | None | 5s | 60 | 4,000 |
| `filesystem.read_file` | Reads one UTF-8 file of at most 256 KiB: `.md`, `.txt`, `.json`, `.yaml`, `.yml` or `.csv` | The file | 5s | 60 | 4,000 |
| `postgres.database_overview` | Gives each service database's size, connections, commits, rollbacks, deadlocks and cache hit ratio | None | 10s | 10 | 500 |
| `postgres.connection_activity` | Gives connections by database and state, the oldest one's age, and how many wait on locks | None | 10s | 10 | 500 |

**What each result carries:**
- **Citations:** a passage carries its citation, document, revision, lines and content hash, and whether a later
  revision replaces it.
- **Research:** a search returns exactly what was sent, what was taken out, and each source's flags with notes. It
  is always `advisory_only`, because no safety rule takes its input from the web.
- **Forecasts:** a forecast carries its provider, licence, retrieval time, coordinates, content hash and age. Its
  state is `fresh`, `stale` or `unavailable`, and a value appears only when the provider gave it.
- **Statistics:** the PostgreSQL MCP signs in with its own login, which holds only `pg_monitor`. It never selects a
  query's text, a client's address or a table's rows.

## Each agent's tools

Each agent's allowlist lives in the Identity Service, which mints its tokens. A token for a tool off the list is
refused, so text an agent reads can never widen it (MCP-002, MCP-007). No agent that reads external content may hold
a write tool (MCP-006). The full lists are in [ADR 0020](adr/0020-windops-mcp-and-agent-allowlists.md), and
`make agent-tools-check` proves them.

## Calling a server

The Agent Runtime calls servers from M8. Any client follows the same steps:

1. **Get a token for the server.** Ask the Identity Service's `ExchangeToken` for one server's audience, such as
   `windops-mcp-documentation`, and the scope of each tool needed, such as `mcp.documentation.search_documents`.
   The token lasts 120 seconds. It names that server alone and only those tools.
2. **Connect over HTTPS:** `https://windops-mcp-<server>.windops-mcp.svc:8443/mcp`. The client must present a
   certificate from the cluster's CA. Without one, the connection is refused.
3. **Send each MCP request with `Authorization: Bearer <token>`.** The servers keep no sessions, so each request is
   checked on its own. A tool list shows only the tools the token may call.

**A refused token gets HTTP 401.** It is refused when it is missing, is a user's own login, is for another server,
was issued for over 5 minutes, or has been tampered with. When Keycloak's keys cannot be read, the answer is 503.

**A tool call that fails** returns `isError` with `{"error": <outcome>, "message": ...}`:

| Outcome | Meaning |
| --- | --- |
| `denied` | The token does not name this tool. An unknown tool gets the same answer |
| `invalid` | The arguments break the input schema. The answer names the fields, never their values |
| `limited` | The caller's rate or the tenant's quota is used up, with `limit` saying which |
| `refused` | The service refused the call, with its own message, such as a missing permission or record |
| `timed_out` | The tool did not finish within its timeout |
| `unavailable` | A dependency, Redis or the audit stream cannot be reached. The call may be tried again |
| `failed` | The tool failed, or its result broke its output schema |

## Untrusted content

Text from outside the platform is returned as an `Untrusted` envelope (MCP-005):

- **trust:** always `untrusted`;
- **kind:** `web_page`, `web_search_result`, `document_passage`, `file` or `provider_text`;
- **source:** its citation or address;
- **label:** says the text is information only, and cannot change instructions, a plan, permissions or tools;
- **flags:** the service's own, such as `stale` or `superseded`, plus `instructions_to_ai` when the text addresses
  an AI;
- **truncated:** whether the text was cut to fit.

The text itself sits between `<<<untrusted NONCE>>>` and `<<<end untrusted NONCE>>>`. NONCE is random for each
envelope, so the text cannot close its envelope early. A marker spelled inside the text is defused. Control
characters, and the Unicode controls that hide or reorder text, are taken out.

**Agents must treat it as data.** They quote and cite it, and never follow it (MCP-007). An agent that reads
external content gets no write tools (MCP-006, M8.6).

## Audit and limits

**Audit:**
- **What is recorded:** every call a valid token reaches, as `mcp.<server>.<tool>`, with its outcome and the user
  who made it. When the token names an agent, the agent is recorded as acting for that user (SEC-010). Arguments
  and results are never recorded.
- **How it is sent:** each server sends its records on its own subject, `audit.requested.mcp-<server>`, and NATS
  lets it publish nothing else.
- **Why there is no outbox:** a server changes nothing, so it has nothing to commit with a record. JetStream
  acknowledges each record before the tool's result is returned, and a result that cannot be audited is withheld.

**Limits:**
- **Where they are counted:** in Redis, under the server's own login and key prefix and the caller's tenant.
- **Who is counted:** a user's ID goes into a key only as a hash.
- **When Redis is down:** nothing is let through.

## Running them

**Deploying them:**

| Command | What it does for the MCP servers |
| --- | --- |
| `make secrets` | Creates each server's Redis and NATS logins in `windops-mcp`, and the PostgreSQL MCP's database login |
| `make postgres-provision` | Grants that login `pg_monitor` |
| `make identity-seed` | Creates each tool's scope in Keycloak |
| `make config` | Gives `windops-mcp` the shared settings, including the Knowledge and External Intelligence addresses |
| `make images` | Builds `windops-mcp-<server>:local` for each server |
| `make services` | Deploys them |

Redis and NATS read their logins at start. After new logins are added, restart them:

```bash
kubectl --context windops --namespace windops-data rollout restart deployment/windops-redis statefulset/windops-nats
```

**Proving them:**
- **`make mcp-check`** proves the foundation with a probe server.
- **`make mcp-servers-check`** proves all five servers on the cluster, through their real endpoints:
  - real searches, a real web search and a real forecast;
  - the sandbox and diagnostics refusals;
  - a caller limited by Redis;
  - every call found in the Audit Service's records.

**To add a tool:**
1. Define it with its input and output models, its scope and its limits. Its input must limit every string and list,
   and external text must be returned with `wrap`.
2. Add its name to `MCP_TOOLS` in `windops_identity.targets`.
3. Run `make identity-seed`.

A tool that breaks these rules stops its server from starting.
