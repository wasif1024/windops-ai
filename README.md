# WindOps AI: AI-Assisted Fault Investigation for Renewable Assets

[![CI](https://github.com/wasif1024/windops-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/wasif1024/windops-ai/actions/workflows/ci.yml)
[![CodeQL](https://github.com/wasif1024/windops-ai/actions/workflows/codeql.yml/badge.svg)](https://github.com/wasif1024/windops-ai/actions/workflows/codeql.yml)

**Agentic AI that investigates equipment faults and prepares maintenance for renewable energy operators,
built first for wind farms.** AI agents interpret operational complexity, deterministic services enforce the rules
that matter, and authorized people approve every high-impact action, for each tenant separately.

The platform is advisory only: it never issues turbine commands. It is a portfolio-grade reference system, built
to production standards and run on synthetic data from a seeded, physics-based turbine simulator.

<table>
  <tr>
    <td width="33%"><img src="docs/assets/industry/offshore-wind-farm.jpg" alt="An offshore wind farm, with rows of white turbines standing in calm blue water."></td>
    <td width="33%"><img src="docs/assets/industry/turbine-technician.jpg" alt="A technician in a helmet and safety harness inspecting equipment inside an open turbine nacelle, high above a forested valley."></td>
    <td width="33%"><img src="docs/assets/industry/control-room.jpg" alt="An operator at a desk in a wind farm control room, with monitoring dashboards on large screens and turbines visible outside at dusk."></td>
  </tr>
  <tr>
    <td align="center"><sub><b>The assets:</b> turbines and whole wind farms</sub></td>
    <td align="center"><sub><b>The field work:</b> inspection and maintenance</sub></td>
    <td align="center"><sub><b>The users:</b> control room operators</sub></td>
  </tr>
</table>

<sub>Illustrative images of the industry this project serves. None shows WindOps AI or its output.
The control room image is a concept illustration: its figures, safety certification label and screens are
not claims about this project, which holds no certifications.</sub>

## Design principles

| Principle | What it means in the system |
| --- | --- |
| **Agents recommend, they never decide** | Agents investigate and draft. The Policy Service, the Approval Service and authorized people make every decision that matters |
| **Deterministic where it counts** | Fault detection, production impact and policy checks are versioned rules with tests, not model output |
| **Evidence before conclusions** | Every investigation works from an immutable evidence snapshot, and every claim must cite its source |
| **Tenant isolation by construction** | The tenant comes from a verified identity, never a request field, and the database enforces it with row-level security |
| **Fail safe, and visibly** | Each dependency has a defined failure behavior: refuse, pause or continue, behind a circuit breaker (ADR 0004) |
| **Observable end to end** | Every request carries one trace ID through logs, metrics and traces, from the gateway to the database |

## Agent orchestration

![Animated walkthrough of the ten stages of an investigation. A deterministic rule detects a gearbox fault and the Investigation Service opens a case with an immutable evidence snapshot. The Supervisor delegates to five specialist agents, which reach services through MCP tools. The result is validated and checked by the Policy Service, a draft work order waits in pending review, an authorized human decides, and the decision is audited.](docs/assets/agent-orchestration.gif)

An investigation from detection to decision, as specified in [SRS section 5.4](docs/srs.md#54-workflow). A
deterministic rule raises the fault; the Supervisor delegates to five specialist agents, each limited to its own
allowlisted tools; the Policy Service checks the result; and a person approves or rejects the draft work order. The
animation illustrates the specified workflow, not a recording.

## Architecture

[![WindOps AI platform architecture: AI-assisted fault investigation for renewable assets. Operators reach the React frontend, and the telemetry simulator sends device samples, both over HTTPS through the APISIX API Gateway, which checks sign-in with Keycloak. In the windops-system namespace, Web BFF calls the Identity, Authorization and Tenant services over gRPC. In windops-application, the Agent Runtime runs a Supervisor and five specialist agents, which reach services only through four MCP servers in windops-mcp. Twenty domain services, grouped by capability, call each other over gRPC with mutual TLS, a signed identity and circuit breakers, and publish durable events to four NATS JetStream streams. Each service owns its PostgreSQL database with row-level security, alongside Redis and Qdrant in windops-data, and external providers are reached through breakers. Every workload sends logs, metrics and traces to the observability stack. A side column lists the security controls on each hop, in place or planned with their module, the circuit breaker's states and failure behaviors, the agent guardrails and where the system runs. A strip shows delivery progress across all 15 phases.](docs/assets/platform-architecture.png)](docs/assets/platform-architecture.png)

<sub>Select the diagram to view it full size. A green dot marks a service running today. Security controls marked
planned name the module that delivers them; tests fail if a control is still shown as planned once its modules are
finished, so the diagram cannot drift from the [implementation tracker](docs/phases/README.md). It is generated by
`uv run python -m scripts.generate.readme_visuals`.</sub>

### Key architectural decisions

| Concern | Decision |
| --- | --- |
| Entry | Apache APISIX is the only way into the cluster: TLS, OIDC token checks, CORS allowlists and per-route rate limits. Devices use a separate route with their own credentials |
| Identity and access | Keycloak for sign-in, with MFA for administrators. Eight roles, scoped to a tenant, wind farm or turbine; the Authorization Service decides every action and denies by default |
| Service communication | 23 services, each with its own image, contracts and data. Synchronous calls use gRPC with versioned Protobuf, mutual TLS, a signed identity and a deadline |
| Events | NATS JetStream, published through a transactional outbox in each service, so a change and its event commit together. Failed messages go to a dead-letter stream |
| Data | PostgreSQL with one database and login per service, and forced row-level security on every tenant table. Telemetry is partitioned by day (ADR 0003). Redis for cache and rate limits, Qdrant for vector search |
| Agents | LangGraph and Claude. Agents reach services only through authenticated, schema-validated MCP servers with per-agent allowlists, and can draft but never approve |
| Resilience | A circuit breaker per caller and dependency, bounded retries for idempotent calls only, and a documented fallback for every dependency (ADR 0004) |
| Observability | OpenTelemetry traces in Tempo, metrics in Prometheus with Alertmanager, logs through Fluent Bit to Loki, and Grafana dashboards |
| Deployment | Kubernetes with Kustomize overlays. Minikube locally (ADR 0001); Azure in the final phase (ADR 0005) |

## Project status

Phases 1 to 3 are complete, each closed by an exit gate run on a real cluster:

- **Phase 1:** the platform foundation.
- **Phase 2:** data and events.
- **Phase 3:** identity and tenancy.

Phase 4, assets and telemetry, is in progress.

| Running today | |
| --- | --- |
| Edge | API Gateway, Web BFF and the React frontend, with Keycloak sign-in and MFA |
| Identity and tenancy | Identity, Authorization and Tenant services, with tenant branding and administration consoles |
| Operations | Asset Service with synthetic demo fleets, and Telemetry Service with authenticated device ingestion |
| Platform | Audit Service, with a tamper-evident trail, plus PostgreSQL, Redis, Qdrant, NATS JetStream and the full observability stack |

The [implementation tracker](docs/phases/README.md) lists every phase and module, and what is finished.

## Documentation

| Document | What it covers |
| --- | --- |
| [Software requirements (SRS v1.1)](docs/srs.md) | Scope, fixed decisions, requirement IDs and acceptance criteria. The source of truth |
| [Implementation tracker](docs/phases/README.md) | All 15 phases and their modules, with progress and exit gates |
| [Architecture](docs/architecture.md) | How a request flows through the system |
| [Microservices](docs/microservices.md) and [service map](docs/service-map.md) | Every service's calls and events, and its behavior when a dependency fails |
| [gRPC contracts](docs/grpc-contracts.md) and [events](docs/events.md) | Versioning, breaking changes, subjects, dead letters and alerts |
| [Security](docs/security.md) and [roles and permissions](docs/rbac.md) | Identities, tenants, data protection and the API, and who may do what |
| [Observability](docs/observability.md) | Data flow, dashboards, alerts and retention |
| [Deployment](docs/deployment.md) | Bootstrapping and running on Kubernetes |
| [Coding standards](docs/coding-standards.md) | Naming, commits, pull requests, merge rules and automated enforcement |

### Architecture decision records

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-local-minikube-profile.md) | A local Minikube profile that runs every workload on a 16 GB laptop |
| [0002](docs/adr/0002-trace-storage-and-kube-state-metrics.md) | Tempo for trace storage, and kube-state-metrics |
| [0003](docs/adr/0003-postgresql-without-timescaledb.md) | PostgreSQL without TimescaleDB, and the local and production database designs |
| [0004](docs/adr/0004-failure-behavior-for-every-dependency.md) | A defined failure behavior for every dependency |
| [0005](docs/adr/0005-azure-deployment-as-the-final-phase.md) | Azure deployment as the final phase, and the services it uses |

## Getting started

### Prerequisites

Docker Desktop, Minikube, kubectl, [uv](https://docs.astral.sh/uv/), [Buf](https://buf.build/docs/installation/),
Node.js 24 and Make. `make dev` checks for each and explains how to install anything missing.

### Run the platform locally

One command runs the backend and the frontend together:

```bash
make dev                       # or scripts/dev/dev.sh
scripts/dev/dev.sh --rebuild   # after pulling backend changes
scripts/dev/dev.sh --trust-ca  # macOS, once: trust the cluster's local CA in the browser
```

It installs the locked Python packages and the frontend's dependencies when they are missing or out of date, starts
the Minikube cluster, and on the first run deploys every workload and seeds the demo tenants, users and assets.
Then it serves:

| Address | What it is |
| --- | --- |
| http://localhost:5173/?tenant=north-sea | The frontend with live reload |
| https://localhost:9443/?tenant=north-sea | The frontend as deployed in the cluster |

Sign-in goes to Keycloak through the gateway, with a certificate from the cluster's own CA. Without `--trust-ca`, the
browser warns once. `make demo-logins` shows the demo users' passwords, and administrators set up an authenticator
app at first sign-in. Ctrl+C stops both servers and leaves the cluster running; `make cluster-stop` frees its
memory. [infrastructure/minikube](infrastructure/minikube/README.md) covers the cluster in detail.

### Develop and verify

```bash
make install   # install the locked development tools
make check     # lint, formatting, type checks, contract checks and tests
make service NAME=example-service   # create a service from the template
make help      # list every command, including the cluster checks
```

Every pull request must pass lint, type checks, tests, CodeQL, a dependency audit, and secret and image scans.
The [coding standards](docs/coding-standards.md) set the merge rules.

### Settings and provider keys

Every value that differs between environments is a setting, so changing one never needs a code change.
[.env.example](.env.example) lists them all with local defaults. `make dev` copies it to `.env` on the first run and
adds new settings later, keeping your values. Git ignores `.env`, and images never contain it.

| Setting | Purpose |
| --- | --- |
| `WINDOPS_PUBLIC_URL` | The gateway's public address, used by Keycloak, token issuers and the browser |
| `WINDOPS_ALLOWED_ORIGINS` | Other browser origins allowed to call the API and sign in, such as the dev server |
| `WINDOPS_KEYCLOAK_REALM`, `WINDOPS_OIDC_AUDIENCE`, `WINDOPS_WEB_CLIENT_ID` | The Keycloak realm, the API's token audience and the browser's client |
| `WINDOPS_ENVIRONMENT`, `WINDOPS_VERSION`, `WINDOPS_LOG_LEVEL` | The release, as logs and traces report it |
| `WINDOPS_*_ADDRESS`, `WINDOPS_OTLP_ENDPOINT`, `WINDOPS_KEYCLOAK_INTERNAL_URL` | Addresses inside the cluster |
| `WINDOPS_KUBE_CONTEXT`, `WINDOPS_DEV_PORT` | Local machine only: the kubectl context and the dev server's port |

After a change, run `make config`. It loads the settings into the `windops-settings` ConfigMap that every workload
reads, and restarts them. Changes to the public address or origins also need `make gateway-routes` and
`make identity-seed`; `make dev` runs both. Settings resolve from the process environment first, then `.env`, then
`.env.example`, so CI and the future Azure pipeline set the same names as pipeline variables, with secrets from Key
Vault.

A setting is never a secret: `make config` refuses any name ending in `_KEY`, `_SECRET`, `_PASSWORD` or `_TOKEN`.
Generated credentials live in Kubernetes Secrets (`make secrets`). Security rules, such as TLS verification, the
Content Security Policy and the SRS rate limits, are not settings, so no value can turn them off. Provider keys stay
blank until a module needs one; add yours to `.env`, and never paste a real key into an issue, pull request or
`.env.example`.

## Repository layout

| Path | Contents |
| --- | --- |
| `frontend/` | React and TypeScript application |
| `services/` | The 23 services, each with its own image, contracts and data boundary |
| `simulator/` | Seeded turbine physics, fault profiles, data problems and scenarios with ground truth |
| `mcp-servers/` | Agent-facing tool servers |
| `agents/` | Supervisor and specialist agent definitions |
| `skills/` | Agent skills |
| `evaluations/` | Datasets, evaluators and regression runs |
| `shared/` | Protobuf contracts and cross-cutting libraries |
| `infrastructure/` | Kubernetes, Minikube, gateway, security and observability configuration |
| `docs/` | Requirements, design documents and architecture decision records |
| `tests/` | Cross-service tests |
| `scripts/` | The commands behind `make`, by role: setup, checks by phase, gates, dev tools and generators. See [scripts/README.md](scripts/README.md) |
