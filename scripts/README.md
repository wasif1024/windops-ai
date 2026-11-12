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
| `checks/identity/` | Phase 3: Keycloak, MFA, mTLS, Authorization, Identity, Tenant, cross-tenant isolation and token exchange | `make mfa-check`, `make cross-tenant-check` |
| `checks/fleet/` | Phase 4: assets, the simulator and telemetry | `make asset-check`, `make telemetry-check`, `make simulator-check`, `make telemetry-storage-check`, `make cms-check`, `make data-quality-check`, `make snapshot-check`, `make freshness-check` |
| `checks/edge/` | The gateway's policy, Web BFF, and the sweep of every route | `make bff-check`, `make route-sweep` |
| `checks/consoles/` | The consoles' APIs, the browser journeys and the Security dashboard | `make frontend-check`, `make security-check` |
| `checks/gates/` | Each phase's exit gate, which runs that phase's checks together | `make phase-3-gate` |
| `dev/` | Working on WindOps: running it locally, the service template, and dead letters | `make dev`, `make service NAME=...`, `make dead-letters` |
| `generate/` | Files made from the code, which tests keep up to date | `make contracts-gen`, `make openapi` |

## Adding one

- **A check** goes in the `checks/` folder of the phase it proves, without a `verify_` prefix, with a `make` target
  ending in `-check`. Import shared code as `from scripts.setup.identity_seed import ...` or
  `from scripts.lib.settings import ...`.
- **Repository paths** come from the file's own place: `Path(__file__).resolve().parents[3]` is the repository root
  for a check in `checks/<area>/`, and `parents[2]` for the other folders.
- **A name a library already has**, such as `redis` or `nats`, is not used for a module, so nothing shadows it. That is
  why the data checks are `redis_cache.py` and `nats_jetstream.py`.
