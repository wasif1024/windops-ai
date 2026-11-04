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

## Status

The repository structure is in place. No services are implemented yet.
Development starts with Phase 1 of the delivery plan.

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
make help      # list every command
```

Follow the [coding standards](docs/coding-standards.md) for naming, commit messages,
pull requests and merge rules.

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
