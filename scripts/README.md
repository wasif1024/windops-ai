# Scripts

The commands behind `make`. Run them through `make`, which is how CI, the phase gates and the docs use them. Each is
a module of the `scripts` package, so it runs from the repository root as, for example,
`uv run python -m scripts.checks.data.audit`. `make help` lists every command.

| Folder | Holds | For example |
| --- | --- | --- |
| `lib/` | Code many scripts share: settings from `.env`, and TLS clients that trust the cluster's CA | `settings.py`, `tls_client.py` |
| `setup/` | What builds and seeds the local cluster: secrets, certificates, data store logins, migrations, settings, gateway routes and the demo users. Checks reuse these too | `make secrets`, `make infra`, `make config`, `make gateway-routes`, `make identity-seed` |
| `checks/foundation/` | Phase 1: the cluster, the service template, the gateway, observability and migrations | `make cluster-check`, `make gateway-check` |
| `checks/data/` | Phase 2: PostgreSQL, Redis, Qdrant, NATS, events, the outbox, the Audit Service and TLS on every store, and Azure Service Bus (Phase 4A) | `make audit-check`, `make data-tls-check`, `make servicebus-check` |
| `checks/identity/` | Phase 3: Keycloak, MFA, mTLS, Authorization, Identity, Tenant, cross-tenant isolation and token exchange | `make mfa-check`, `make cross-tenant-check`, `make policy-change-check` |
| `checks/fleet/` | Phases 4 to 10: assets, the simulator, telemetry, alarms, detection, cases, policy, maintenance, inventory and approval | `make asset-check`, `make telemetry-check`, `make simulator-check`, `make telemetry-storage-check`, `make cms-check`, `make data-quality-check`, `make snapshot-check`, `make freshness-check`, `make fault-stream-check`, `make alarm-check`, `make fault-rules-check`, `make detection-check`, `make case-check`, `make fault-case-check`, `make maintenance-check`, `make inventory-check`, `make approval-check`, `make stale-check`, `make policy-check`, `make impact-check`, `make weather-check`, `make weather-outage-check` |
| `checks/edge/` | The gateway's policy, Web BFF, and the sweep of every route | `make bff-check`, `make route-sweep` |
| `checks/consoles/` | The consoles' APIs, the browser journeys and the Security dashboard | `make frontend-check`, `make security-check` |
| `checks/agents/` | Phases 8 and 9: the agent stack's vendor checks, agent runs as queued jobs, the investigation graph, agents' tools, the team, step 12's planner, its traces, its Sandbox tasks, live progress, and the Claude policy | `make vendor-check`, `make runtime-check`, `make workflow-check`, `make agent-tools-check`, `make team-check`, `make planner-check`, `make trace-check`, `make sandbox-check`, `make progress-check`, `make phase-8-gate`, `make claude-check` |
| `evaluation/` | Phase 8: the regression set's scenario loader, its deterministic evaluators (EVL-003), and the runner that replays scenarios on the cluster and keeps the scores in LangSmith | `make regression` |
| `checks/gates/` | Each phase's exit gate, which runs that phase's checks together | `make phase-3-gate`, `make phase-4-gate`, `make phase-5-gate`, `make phase-6-gate`, `make phase-7-gate`, `make boundary-check`, `make citation-check` |
| `dev/` | Working on WindOps: running it locally, the service template, and dead letters | `make dev`, `make service NAME=...`, `make dead-letters` |
| `generate/` | Files made from the code, which tests keep up to date, and the README's pictures, including the console captured from the running platform | `make contracts-gen`, `make openapi`, `make readme-screens` |

## Adding one

- **A check** goes in the `checks/` folder of the phase it proves, without a `verify_` prefix, with a `make` target
  ending in `-check`. Import shared code as `from scripts.setup.identity_seed import ...` or
  `from scripts.lib.settings import ...`.
- **Repository paths** come from the file's own place: `Path(__file__).resolve().parents[3]` is the repository root
  for a check in `checks/<area>/`, and `parents[2]` for the other folders.
- **A name a library already has**, such as `redis` or `nats`, is not used for a module, so nothing shadows it. That is
  why the data checks are `redis_cache.py` and `nats_jetstream.py`.
