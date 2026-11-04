# WindOps AI

Agentic AI for wind farm operations and predictive maintenance. AI interprets
operational complexity, deterministic systems enforce critical rules, and
authorized humans approve high-impact actions.

This is a portfolio-grade platform that runs on synthetic data. It is advisory
only and never issues turbine commands.

## Agent orchestration

![Animated walkthrough of the ten stages of an investigation. A deterministic rule detects a gearbox fault and the Investigation Service opens a case with an immutable evidence snapshot. The Supervisor delegates to five specialist agents, which reach services through MCP tools. The result is validated and checked by the Policy Service, a draft work order waits in pending review, an authorized human decides, and the decision is audited.](docs/assets/agent-orchestration.gif)

Agents recommend and draft. Deterministic services and authorized humans decide.
The animation illustrates the workflow in
[SRS section 5.4](docs/srs.md#54-workflow), not a recording of a running system.

## Architecture

[![Architecture diagram. Users reach a React frontend, API Gateway and Web BFF. Agent Runtime runs the Supervisor and five specialist agents, which use MCP servers to call 23 gRPC services in total. Services exchange durable events over NATS JetStream, store data in PostgreSQL, TimescaleDB, Redis and Qdrant, and use external providers including Claude, Tavily and Open-Meteo.](docs/assets/architecture.png)](docs/assets/architecture.png)

This is the target design from the SRS. To update either visual, edit and run
`scripts/generate_readme_visuals.py`.

[Service map](docs/service-map.md) shows how all 23 services connect, and what happens when a circuit
breaker opens.

## System and security architecture

[![Layers from the client to external providers, with the security controls at each. The React frontend reaches the API Gateway over HTTPS, and Web BFF calls services over gRPC with a signed identity, deadline and trace. Agents reach services only through MCP tools. Each service reaches only its own data, and shares events through NATS JetStream. Kubernetes surrounds the edge, agents, services and data, and only named paths leave the cluster. Each layer lists its controls, marked as in place today or planned with the module that delivers them. A strip shows the progress of all 14 delivery phases.](docs/assets/security-layers.png)](docs/assets/security-layers.png)

A green dot marks a control that is built and tested in the shared code and templates. No real
business service runs yet, so these apply once services are built on them. A hollow amber dot marks a
planned control, with the module that delivers it. Tests fail if a cited module does not exist, or if a
control is still shown as planned after its modules are finished.

### For architects

| Document | What it covers |
| --- | --- |
| [Requirements (SRS v1.1)](docs/srs.md) | Scope, fixed decisions, requirement IDs and acceptance criteria |
| [Implementation tracker](docs/phases/README.md) | All 14 phases and their modules, with what is finished |
| [ADR 0001: Local Minikube profile](docs/adr/0001-local-minikube-profile.md) | Running every workload on a 16 GB laptop |
| [ADR 0002: Trace storage](docs/adr/0002-trace-storage-and-kube-state-metrics.md) | Adding Tempo and kube-state-metrics |
| [gRPC contracts](docs/grpc-contracts.md) | Versioning, breaking changes and per-call rules |
| [Observability](docs/observability.md) | Data flow, dashboards and retention by environment |
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
