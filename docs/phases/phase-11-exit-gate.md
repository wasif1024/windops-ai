# Phase 11 exit gate

**Result:** passed on 2026-11-26, on the local Minikube cluster, in 20 minutes, at no Claude cost. Every
investigation was rehearsed. The knowledge search made one OpenAI embedding call per screen size.

SRS section 12 sets the gate: *role-based end-to-end browser tests cover every Section 4.11 screen.*

```bash
make frontend-check && make browser-coverage-check   # the gate itself, about 10 minutes
make phase-11-gate                                     # the gate, then every check for what Phase 11 changed
```

## How the gate is proved

- **Each journey names its role and the screens it covers** (`frontend/e2e/coverage.ts`), as Playwright
  annotations. The 29 screens are SRS 4.11's list, item by item.
- **Each journey checks its role's menu** as it signs in, against `frontend/e2e/role-menus.json`. That file is worked
  out from the SRS 3.2 matrix in the Authorization Service's policy and the permissions each console screen needs,
  and `tests/test_role_menus.py` keeps it matching them. So a role is shown exactly its screens, and no more.
- **Every test user holds one role**, made by the check for each screen size and deleted afterwards, or the demo
  tenant's own operator. Each signs in through the gateway and Keycloak.
- **`make browser-coverage-check`** reads Playwright's JSON report, and passes only when every journey passed, every
  screen had a passing journey on a desktop and on a phone (UX-001), and every SRS 3.2 role signed in.
  `tests/test_browser_coverage.py` proves it fails on a missing size, a missing role and a failed journey.

## The coverage

```text
ok - Fleet overview: control-room-operator, operations-manager (desktop, phone)
ok - Turbine detail: control-room-operator (desktop, phone)
ok - Telemetry charts: control-room-operator (desktop, phone)
ok - Alarm console: control-room-operator (desktop, phone)
ok - Investigation list: operations-manager, reliability-engineer (desktop, phone)
ok - Investigation timeline: reliability-engineer (desktop, phone)
ok - Live progress: reliability-engineer (desktop, phone)
ok - Findings: reliability-engineer (desktop, phone)
ok - Evidence viewer: reliability-engineer (desktop, phone)
ok - Production impact view: control-room-operator (desktop, phone)
ok - Weather and source freshness panel: control-room-operator (desktop, phone)
ok - Maintenance planner: maintenance-planner (desktop, phone)
ok - Parts lookup: maintenance-planner (desktop, phone)
ok - Approval queue: maintenance-manager (desktop, phone)
ok - Knowledge library: auditor, reliability-engineer, tenant-administrator (desktop, phone)
ok - Audit explorer: auditor, tenant-administrator (desktop, phone)
ok - Notification center: control-room-operator, maintenance-manager (desktop, phone)
ok - Administrator console: users: tenant-administrator (desktop, phone)
ok - Administrator console: roles: tenant-administrator (desktop, phone)
ok - Administrator console: permissions: tenant-administrator (desktop, phone)
ok - Administrator console: MFA: tenant-administrator (desktop, phone)
ok - Administrator console: sessions: tenant-administrator (desktop, phone)
ok - Administrator console: branding: tenant-administrator (desktop, phone)
ok - Administrator console: approval policies: tenant-administrator (desktop, phone)
ok - Administrator console: MCP integrations: tenant-administrator (desktop, phone)
ok - Administrator console: gateway policies: tenant-administrator (desktop, phone)
ok - Platform console: list, create and suspend tenants: platform-administrator (desktop, phone)
ok - Platform console: tenants' branding and domains: platform-administrator (desktop, phone)
ok - Platform console: break-glass access: platform-administrator (desktop, phone)
All 29 SRS 4.11 screens are covered on a desktop and a phone, by 26 passing journeys as 8 roles, each menu exactly its screens
```

## The 26 journeys

```text
[desktop] › e2e/admin.spec.ts:23:1 › a Tenant Administrator manages users, roles and permissions, with step-up
[desktop] › e2e/alarms.spec.ts:13:1 › an operator finds a new alarm, acknowledges it, and sees it in its history
[desktop] › e2e/approvals.spec.ts:17:1 › a manager reviews a submitted work order and approves it
[desktop] › e2e/fleet.spec.ts:26:1 › an operator watches the fleet, a turbine's readings and its live charts
[desktop] › e2e/impact.spec.ts:17:1 › an operator reads the forecast and its freshness, then estimates a case's impact
[desktop] › e2e/investigations.spec.ts:18:1 › an engineer investigates a turbine, follows it live, and challenges a finding
[desktop] › e2e/knowledge.spec.ts:45:1 › an engineer browses, searches and reads the library
[desktop] › e2e/knowledge.spec.ts:80:1 › a Tenant Administrator uploads a document addressed to an AI, and it is held
[desktop] › e2e/planner.spec.ts:26:1 › a planner finds a part, drafts a work order with it, and submits it
[desktop] › e2e/platform.spec.ts:21:1 › a Platform Administrator manages tenants and break-glass access, with step-up
[desktop] › e2e/roles.spec.ts:16:1 › an auditor reads the trail and the library, and changes nothing
[desktop] › e2e/roles.spec.ts:36:1 › an operations manager follows the fleet and its cases
[desktop] › e2e/shell.spec.ts:33:1 › a control room operator signs in to a branded, role-aware shell
[phone] › e2e/admin.spec.ts:23:1 › a Tenant Administrator manages users, roles and permissions, with step-up
[phone] › e2e/alarms.spec.ts:13:1 › an operator finds a new alarm, acknowledges it, and sees it in its history
[phone] › e2e/approvals.spec.ts:17:1 › a manager reviews a submitted work order and approves it
[phone] › e2e/fleet.spec.ts:26:1 › an operator watches the fleet, a turbine's readings and its live charts
[phone] › e2e/impact.spec.ts:17:1 › an operator reads the forecast and its freshness, then estimates a case's impact
[phone] › e2e/investigations.spec.ts:18:1 › an engineer investigates a turbine, follows it live, and challenges a finding
[phone] › e2e/knowledge.spec.ts:45:1 › an engineer browses, searches and reads the library
[phone] › e2e/knowledge.spec.ts:80:1 › a Tenant Administrator uploads a document addressed to an AI, and it is held
[phone] › e2e/planner.spec.ts:26:1 › a planner finds a part, drafts a work order with it, and submits it
[phone] › e2e/platform.spec.ts:21:1 › a Platform Administrator manages tenants and break-glass access, with step-up
[phone] › e2e/roles.spec.ts:16:1 › an auditor reads the trail and the library, and changes nothing
[phone] › e2e/roles.spec.ts:36:1 › an operations manager follows the fleet and its cases
[phone] › e2e/shell.spec.ts:33:1 › a control room operator signs in to a branded, role-aware shell
```

## Every check

`make phase-11-gate` paused the observability stack for the run, waited for a calm node before each check, and
restored the stack at the end.

```text
ok - frontend-check (612 s): every console journey, as each role, on a desktop and a phone
ok - browser-coverage-check (0 s): every SRS 4.11 screen covered on both, and every role's menu
ok - knowledge-check (34 s): the library browsed within permissions, uploads screened and held
ok - retrieval-check (24 s): search and citations, scoped by wind farm, across revisions
ok - audit-check (125 s): a tamper-evident audit record, now filtered for the explorer
ok - notification-check (48 s): who is told what, read in the notification center
ok - approval-check (69 s): the queue's standing, and only a person decides on what is current
ok - impact-check (48 s): the production impact figures the impact view shows
ok - weather-check (13 s): the forecast and its freshness the weather panel shows
ok - bff-check (14 s): Web BFF's routes
ok - cross-tenant-check (135 s): every service's database and API attacked across tenants
ok - migrate-check (123 s): migration Jobs, the audit index among them, run once per release
Phase 11 exit gate passed in 20 minutes: all 12 checks
```

## What Phase 11 built

| Module | What | PR |
| --- | --- | --- |
| M11.1 | Investigation screens: list, live progress, findings with reviews, timeline, verification | #148 |
| M11.2 | Evidence viewer: snapshot contents, hashes, data quality and citations | #149 |
| M11.3 | Production impact view, and the weather and source freshness panel | #150 |
| M11.4 | Approval queue: eligible decisions, stale and superseded states | #151 |
| M11.5 | Knowledge library: browsing and management within permissions | #152 |
| M11.6 | Audit explorer and notification center | #153 |
| M11.7 | Full administrator console: approval policy, change review, MCP integrations, gateway policies | #154 |
| M11.8 | This gate | #155 |

## Known limits

- **Grants left by check users:** each check run makes test users with one role, deletes the users, and keeps their
  grants, as the audit needs (RBAC-005). They pile up: notifications of every submission go to every one of them, and
  the administrator console's approval policy lists them, flagged as no longer users. They should be revoked by the
  checks' clean-up, or in Phase 14.
- **The node:** on the 6-CPU local node, a browser run beside the observability stack could starve Postgres of CPU
  until its probe restarted it, failing every sign-in. The gate pauses the stack for the run.
