# windops-mcp-windops

WindOps MCP (SRS 6.3): the platform's own records, for agents, in the caller's tenant. It is built on the shared
`windops-mcp` library, so every control in [ADR 0014](../../docs/adr/0014-mcp-tools-behind-per-call-tool-tokens.md)
applies to every call. Its tools are in [ADR 0020](../../docs/adr/0020-windops-mcp-and-agent-allowlists.md).

| Tool | Service | What it returns |
| --- | --- | --- |
| `get_turbine` | Asset | A turbine's wind farm, model, rating and status |
| `get_latest_telemetry` | Telemetry | Its latest reading of each metric, with its unit and quality label |
| `get_telemetry_summary` | Telemetry | A frozen telemetry snapshot's readings, summarized per metric, each citable as `<snapshot>#<metric>` (ADR 0028) |
| `list_alarms` | Alarm | Its alarms, newest first |
| `get_case` | Investigation | A case, with its current evidence snapshot |
| `list_cases` | Investigation | A turbine's cases: its fault history |
| `get_evidence` | Investigation | A frozen evidence snapshot, item by item |
| `estimate_impact` | Production Impact | A deterministic estimate, each figure with its range and assumptions |
| `check_policy` | Policy | The deterministic rules' outcome on a proposed action |
| `create_draft_work_order` | Maintenance | A draft work order, validated at once: the one change an agent may make (ADR 0029) |

**Tenant and permissions:**
- **Tenant:** every tool calls its service as the verified caller, and the tenant comes from the token. No tool takes
  a tenant.
- **Permissions:** each service decides what the caller may read. An agent's caller is held to reading and drafting
  (SEC-009).
- **Outputs:** they name only the fields an agent needs. No user's ID reaches one.

**Nothing here changes a record.** `estimate_impact` and `check_policy` keep the result they compute, as their
services always do, so a later step can cite it. A retry by the same caller gets the same result. Parts and drafts
come with the Inventory and Maintenance services (Phase 9).

## Check it

```bash
uv run pytest mcp-servers/windops-mcp/tests
make agent-tools-check   # on the cluster
```
