# ADR 0041: MCP integrations and gateway policies, shown in the console and changed by pull request

**Status:** Accepted, 2026-11-26, for M11.7.

## Context

- **SRS 9.4:** administrators review changes to users, roles, permissions, approval policies, MCP integrations and
  gateway policies. SRS 4.11 lists these in the administrator console.
- **Approval policy** is already the role permissions: which roles hold `maintenance.approve` (ADR 0033). A change to
  it waits for a second administrator (ADR 0035), and is audited.
- **MCP integrations** are the MCP servers and their tools, each with a timeout, a per-caller rate and a per-tenant
  daily quota, and the agents allowed each tool (ADR 0014, ADR 0020). **Gateway policies** are the APISIX routes:
  what each lets in, sign-in, limits, CORS and timeouts. Both are platform configuration in code and manifests,
  changed by pull request, reviewed there, and deployed with a release. No tenant can change them.
- **The user chose,** on 2026-11-26: show them and review them; do not make them tenant-managed.

## Decision

- **A platform catalog** is generated from the sources by `scripts/generate/platform_catalog.py`: each MCP
  server's own tool definitions, the Identity Service's agent allowlists, the development-only servers' kustomization,
  the gateway route files and Web BFF's per-person limits. It is committed into Web BFF's package as
  `platform_catalog.json`, and `tests/test_platform_catalog.py` fails when it no longer matches them. So a change to
  any of them shows in the catalog's diff, in the same pull request.
- **Web BFF serves it** at `GET /api/v1/admin/platform-catalog` with the release it was built into, to whoever the
  Authorization Service says holds `audit.read`, the reviewer's permission. It holds upstream names, so it is not for
  everyone signed in.
- **The administrator console** gains:
  - **Approval policy:** the roles that approve, who holds them where (a grant to someone no longer a user is
    flagged), and the changes waiting for a second administrator.
  - **Change review:** the audit trail's changes to users, sessions, roles, role permissions, approval policy,
    break-glass, branding, notification rules and knowledge settings, by kind, newest first, by who made them.
  - **MCP integrations** and **Gateway policies:** the catalog, read-only, saying how they change.
- **Auditors** keep the audit explorer, not the administrator console, as before.

## Consequences

- What the console shows is what the release holds, never a copy that can drift from it.
- A tenant cannot switch off a tool for its agents or lower its own limits. That would need storage, enforcement in
  the MCP library and the BFF limiter, and a second administrator's confirmation: a later module if wanted.
- A tenant's use of each tool today is not shown: it is counted in the MCP servers' own Redis keys, which Web BFF is
  kept from reading.
