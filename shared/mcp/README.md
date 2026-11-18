# windops-mcp

The foundation every WindOps MCP server is built on. A server names its tools; this library applies
the same controls to every call ([ADR 0014](../../docs/adr/0014-mcp-tools-behind-per-call-tool-tokens.md)):

- **Authenticated callers (MCP-001, MCP-004):** each call carries a Keycloak token exchanged for this
  one server, through the Identity Service. It lasts 120 seconds, names only this server in its
  audience, and names each tool it may call as a scope, `mcp.<server>.<tool>`. A user's own login,
  a token for another server, or one issued for longer than five minutes is refused.
- **Schemas (MCP-003):** a tool's input refuses unknown fields, and every string and list in it has a
  size limit, or the server will not start. Its result is checked against its output schema before
  it is returned.
- **Limits (MCP-003):** a timeout per tool, a rate per caller each minute and a quota per tenant each
  day, counted in Redis and refused when they cannot be counted.
- **Audit (MCP-003):** every call a token got through to is sent to the Audit Service with its
  outcome, never its arguments or result. A result that cannot be audited is withheld.
- **Untrusted content (MCP-005):** a tool that returns text from outside the platform must return it
  `wrap`ped: labelled as data that cannot change instructions, a plan, a permission or a tool list,
  between markers the text cannot forge, and flagged when it addresses an AI.

A tool's handler runs as its verified caller: `acting_for_caller` carries the user's context to
domain services, which decide what that user may see.

```bash
uv run pytest shared/mcp/tests
```
