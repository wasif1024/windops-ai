# Phase 7 exit gate

**Result:** passed on 2026-11-18, on the local Minikube cluster, with all 14 checks, in 10 minutes.

SRS section 12 sets the gate: *results carry citations and provenance, and permissions are enforced before return.*
Run it yourself with:

```bash
make dev
make knowledge-seed
make phase-7-gate
```

The cluster needs to reach OpenAI, Tavily and Open-Meteo, with `OPENAI_API_KEY` and `TAVILY_API_KEY` set in `.env`.
The gate makes a few real web searches.

## Through the MCP servers, as an agent will

`make citation-check` proves the gate the way agents will meet it from Phase 8: through the Documentation, Tavily
and Weather MCP servers. Each user signs in to Keycloak, and the Identity Service exchanges their token for each
server. Every call then goes over mutual TLS, through the official MCP client.

**What it checks:**
- **Citations:** every passage found for six demo questions is cited `kn:<document>@<revision>#<n>`. Each one:
  - names the document and revision its citation does;
  - is a revision in the synthetic set's manifest for that tenant;
  - is current;
  - holds exactly the text its SHA-256 hash says.

  Read again by its citation, it gives the same text and hash.
- **Provenance:**
  - **Web research:** a web search names what it sent, and each source's address, domain, retrieval time and
    flags, each flag with its note. Read again, it gives the same sources.
  - **Forecasts:** a forecast names its provider, licence, retrieval time, valid span and hash.
- **Permissions before return:** each user below gets a refusal and nothing else: no passage, source or value.
  - an Atlas Wind operator searching for, or reading, North Sea documents;
  - a reliability engineer scoped to `dogger-east`, who is made for the run, searching the whole tenant or
    `hornsea-west`, or researching a turbine there;
  - a user with no role at all, also made for the run, at any server.

  The two users made for the run are removed afterwards, along with the grant.

Output from the gate run:

```text
ok - All 25 passages found for 6 questions, from 7 documents, are cited, in the manifest, current, and hold exactly their hashed text, read again by citation
ok - A web search names what it sent and its 5 sources' addresses, domains, retrieval times and flags, each flag with its note
ok - Read again, the search gives the same sources, as recorded
ok - A fresh forecast names its provider, licence, retrieval time, valid span and hash
ok - A user of atlas-wind finds 6 documents of their own, never one of north-sea's
ok - Nor any of 6 of north-sea's own citations, which return nothing
ok - Scoped to dogger-east, a user searches there, never the whole tenant or hornsea-west
ok - Nor may they research a turbine of hornsea-west
ok - A user with no role gets nothing from Documentation, Tavily or Weather
Citation check passed
```

## Underneath: each service enforces it before return

The MCP servers hold no data of their own. The guarantees come from the services behind them, which their own checks
prove directly:

| Guarantee | Where it is enforced | Proved by |
| --- | --- | --- |
| A passage's permission is decided before its text is read | The Knowledge Service asks the Authorization Service for each document ([ADR 0012](../adr/0012-document-permissions-and-current-revisions.md)) | `retrieval-check` |
| Only the caller's tenant is searched | A filter on every Qdrant query, set by the server, then PostgreSQL's row-level security | `retrieval-check`, `cross-tenant-check` |
| Replaced revisions are left out unless asked for, and then marked | The Knowledge Service | `retrieval-check` |
| A citation always reads the same text | Revisions are never changed, and re-indexing keeps each citation | `retrieval-check` |
| A search records what was sent and every source, unchanged | The External Intelligence Service ([ADR 0013](../adr/0013-web-research-made-generic-and-advisory.md)) | `research-check` |
| A tool runs only for a token issued for that server and tool | `windops-mcp` ([ADR 0014](../adr/0014-mcp-tools-behind-per-call-tool-tokens.md)) | `mcp-check`, `mcp-servers-check` |
| External text is never presented as an instruction | It is wrapped and labelled untrusted, and flagged when it addresses an AI | `mcp-servers-check`, `citation-check` |

## Every Phase 7 check, and the earlier ones Phase 7 touched

Phase 7 changed several things earlier phases depend on:
- token exchange, which now issues tool scopes;
- the Authorization Service's service grants, for Knowledge and External Intelligence;
- the audit trail, which the new services and the MCP servers write to;
- tenant-owned tables in two new databases;
- the demo models, renamed in M7.1, which Asset and Production Impact's curves use;
- the forecast the Weather MCP serves.

So the gate runs the checks covering those with its own:

| Check | Proves | Time |
| --- | --- | --- |
| `citation-check` | Results carry citations and provenance, and permissions hold before return, through MCP | 12 s |
| `knowledge-check` | Uploads screened, documents held for release, and embedded through the adapter | 55 s |
| `retrieval-check` | Cited passages of current revisions, filtered by tenant and permission | 40 s |
| `research-check` | Web research made generic, recorded, flagged and limited | 52 s |
| `mcp-check` | Tool tokens for one server and its tools, checked on every call | 2 s |
| `mcp-servers-check` | Five MCP servers, over mutual TLS, limited in Redis and audited | 45 s |
| `token-exchange-check` | Short-lived tokens for one service, and only for their owner | 1 s |
| `weather-check` | A real Open-Meteo forecast, with its provenance and freshness | 22 s |
| `authorization-check` | Roles, scopes, break-glass, service grants and audited denials | 95 s |
| `audit-check` | A tamper-evident audit record | 104 s |
| `cross-tenant-check` | Every data path attacked across tenants, and refused | 7 s |
| `asset-check` | The renamed demo models, and assets read by role and scope | 20 s |
| `impact-check` | Estimates from the renamed models' power curves, judged by Policy | 49 s |
| `migrate-check` | Migration Jobs run once per release | 146 s |

All 14 passed, with nothing found to fix on the way.

**`make check` passed:** lint, types, contracts and 2,124 tests.
- **Two flaky tests, not counted as failures:** during the runs, a Policy Service test and an Identity Service
  test each failed once and passed when run again. Each was timing-sensitive, and one failure came while the gate
  was running on the same machine. Neither area changed in Phase 7. The final run passed in full.

**Not run again:** the checks for what Phase 7 didn't touch:
- the gateway's policies;
- Keycloak's MFA flows;
- the frontend;
- the telemetry pipeline;
- the Phase 5 fault-to-case path;
- Phase 6's boundary tests and weather outage.

## Next

Phase 8, the agent workflow, starts with M8.1: confirming Claude's features through Deep Agents, Claude's data
handling, and LangSmith Sandbox's availability.
