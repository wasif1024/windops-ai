# WindOps AI

Agentic AI for wind farm operations and predictive maintenance. AI interprets
operational complexity, deterministic systems enforce critical rules, and
authorized humans approve high-impact actions.

This is a portfolio-grade platform that runs on synthetic data. It is advisory
only and never issues turbine commands.

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

<sub>Illustrative images of the industry this project is for. None shows WindOps AI or its output. The
control room image is a concept illustration: its figures, safety certification label and screens are
not claims about this project, which holds no certifications.</sub>

## Agent orchestration

![Animated walkthrough of the ten stages of an investigation. A deterministic rule detects a gearbox fault and the Investigation Service opens a case with an immutable evidence snapshot. The Supervisor delegates to five specialist agents, which reach services through MCP tools. The result is validated and checked by the Policy Service, a draft work order waits in pending review, an authorized human decides, and the decision is audited.](docs/assets/agent-orchestration.gif)

Agents recommend and draft. Deterministic services and authorized humans decide.
The animation illustrates the workflow in
[SRS section 5.4](docs/srs.md#54-workflow), not a recording of a running system.

## Architecture

[![Service overview. The React frontend and telemetry simulator enter through the APISIX API Gateway, which checks tokens with Keycloak. Web BFF and Agent Runtime, through MCP servers, call 23 services in total over gRPC. Services publish and consume durable events through NATS JetStream. Each of 20 services has its own database and login, on one PostgreSQL pod, alongside Redis and Qdrant. Every workload sends logs, metrics and traces to the observability stack, and external providers are reached through circuit breakers.](docs/assets/service-overview.png)](docs/assets/service-overview.png)

This overview shows every service, where its data lives and how events move. The next diagram shows the
agents and how each kind of service relates to them.

[![Architecture diagram. Users reach a React frontend, API Gateway and Web BFF. Agent Runtime runs the Supervisor and five specialist agents, which use MCP servers to call 23 gRPC services in total. Services exchange durable events over NATS JetStream, store data in PostgreSQL, Redis and Qdrant, and use external providers including Claude, Tavily and Open-Meteo.](docs/assets/architecture.png)](docs/assets/architecture.png)

This is the target design from the SRS. To update either visual, edit and run
`scripts/generate_readme_visuals.py`.

[Service map](docs/service-map.md) shows how all 23 services connect, and what happens when a circuit
breaker opens.

## How it works end to end

[![End-to-end architecture in four parts. First, all 23 services by Kubernetes namespace, showing gRPC calls, JetStream events, and data in one PostgreSQL pod with 20 separate databases. Second, one gRPC call step by step, with the security on each hop marked as built or planned. Third, the circuit breaker's closed, open and half-open states, and what happens when each dependency fails. Fourth, the shared local database server beside the planned production design with standby servers and backups.](docs/assets/end-to-end-architecture.png)](docs/assets/end-to-end-architecture.png)

This shows how services communicate, where the databases run, how each hop is secured and how circuit
breakers respond to a failure. Select it to see it full size.

## System and security architecture

[![Layers from the client to external providers, with the security controls at each. The React frontend reaches the API Gateway over HTTPS, and Web BFF calls services over gRPC with a signed identity, deadline and trace. Agents reach services only through MCP tools. Each service reaches only its own data, and shares events through NATS JetStream. Kubernetes surrounds the edge, agents, services and data, and only named paths leave the cluster. Each layer lists its controls, marked as in place today or planned with the module that delivers them. A strip shows the progress of all 14 delivery phases.](docs/assets/security-layers.png)](docs/assets/security-layers.png)

A green dot marks a control that is built and tested in the shared code and templates. Only the Audit
Service and sample services run so far, so most apply once services are built on them. A hollow amber dot marks a
planned control, with the module that delivers it. Tests fail if a cited module does not exist, or if a
control is still shown as planned after its modules are finished.

### For architects

| Document | What it covers |
| --- | --- |
| [Requirements (SRS v1.1)](docs/srs.md) | Scope, fixed decisions, requirement IDs and acceptance criteria |
| [Implementation tracker](docs/phases/README.md) | All 15 phases and their modules, with what is finished |
| [ADR 0001: Local Minikube profile](docs/adr/0001-local-minikube-profile.md) | Running every workload on a 16 GB laptop |
| [ADR 0002: Trace storage](docs/adr/0002-trace-storage-and-kube-state-metrics.md) | Adding Tempo and kube-state-metrics |
| [ADR 0003: PostgreSQL without TimescaleDB](docs/adr/0003-postgresql-without-timescaledb.md) | Telemetry storage, and the shared local and production database designs |
| [ADR 0005: Azure deployment](docs/adr/0005-azure-deployment-as-the-final-phase.md) | The final phase, and the Azure services it uses |
| [Service certificates](infrastructure/security/pki/README.md) | Mutual TLS between services, and how certificates renew |
| [Frontend](frontend/README.md) | The browser's application: sign-in, branding and role-aware screens |
| [Keycloak](infrastructure/security/keycloak/README.md) | Login, tenants, token rules and the tenant model vendor check |
| [Service map](docs/service-map.md) | Every service's calls and events, and each failure behavior |
| [gRPC contracts](docs/grpc-contracts.md) | Versioning, breaking changes and per-call rules |
| [Observability](docs/observability.md) | Data flow, dashboards and retention by environment |
| [Events](docs/events.md) | Subjects, failure handling, dashboard and alerts |
| [Coding standards](docs/coding-standards.md) | Merge rules, and what is enforced automatically |

## Status

Phase 1 (platform foundation) is complete, and its [exit gate](docs/phases/phase-01-exit-gate.md)
passed on a real local cluster. The shared libraries, service template, CI, API gateway and
observability stack are built and tested, and two sample services prove them. No business services
exist yet. Phase 2 (data and events) is next.
The [tracker](docs/phases/README.md) shows the current position.

## Requirements

[docs/srs.md](docs/srs.md) is the repository copy of SRS v1.1, the source of
truth for scope and architecture. Section 12 sets the delivery order.

[docs/phases/README.md](docs/phases/README.md) tracks progress through the 14
phases and their modules.

## Development

Install [uv](https://docs.astral.sh/uv/), [Buf](https://buf.build/docs/installation/) and
Make, then run:

```bash
make install   # install the locked development tools
make check     # run lint, formatting, type checks, contract checks and tests
make service NAME=telemetry-service   # create a service from the template
make help      # list every command
```

### Run it and see it

One command runs the backend and the frontend together, so you can watch the screens as they are built:

```bash
make dev                    # or scripts/dev.sh
scripts/dev.sh --rebuild    # after pulling backend changes
scripts/dev.sh --trust-ca   # macOS, once: trust the cluster's local CA, so the browser shows no warning
```

It needs Docker Desktop, Minikube, kubectl, uv, Node.js 24 and Make, and says how to install any that are missing.
Then it:

1. installs the locked Python packages (`uv sync`) and the frontend's `node_modules` (`npm ci`), when they are
   missing or out of date,
2. starts the Minikube cluster if it is stopped,
3. the first time, deploys everything and seeds the demo tenants and users, which takes a while,
4. runs the gateway at https://localhost:9443 and the Vite dev server at http://localhost:5173, and prints the URLs
   and the demo sign-ins.

| Open | What it is |
| --- | --- |
| http://localhost:5173/?tenant=north-sea | The frontend with live reload: a saved change shows at once |
| https://localhost:9443/?tenant=north-sea | The frontend as deployed in the cluster |

Sign-in goes to Keycloak at https://localhost:9443, whose certificate comes from your cluster's own CA. Without
`--trust-ca`, the browser warns once: choose Advanced, then Proceed to localhost. Administrators set up an
authenticator app at their first sign-in. Ctrl+C stops both, and the cluster keeps running: `make cluster-stop`
frees its memory.

Follow the [coding standards](docs/coding-standards.md) for naming, commit messages,
pull requests and merge rules. To run on a local cluster, see
[infrastructure/minikube](infrastructure/minikube/README.md).

### Provider keys

No keys are needed yet. When a module needs one, copy the template and add your keys:

```bash
cp .env.example .env
```

Git ignores `.env`, and images never receive it. Never paste a real key into an issue, a pull
request, a chat or `.env.example`. [.env.example](.env.example) shows which module needs each key.

## Repository layout

| Path | Contents |
| --- | --- |
| `frontend/` | React and TypeScript application |
| `services/` | The 23 services, each with its own image and data boundary |
| `simulator/` | Synthetic telemetry and fault scenarios |
| `mcp-servers/` | Agent-facing tools |
| `agents/` | Supervisor and specialist agent definitions |
| `skills/` | Agent skills |
| `evaluations/` | Datasets, evaluators and regression runs |
| `shared/` | Protobuf contracts and cross-cutting libraries |
| `infrastructure/` | Docker, Kubernetes, Minikube and observability configuration |
| `docs/` | SRS, design documents and architecture decision records |
| `tests/` | Cross-service tests |
| `scripts/` | Developer scripts |
