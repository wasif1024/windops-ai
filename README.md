# WindOps AI

Agentic AI for wind farm operations and predictive maintenance. AI interprets
operational complexity, deterministic systems enforce critical rules, and
authorized humans approve high-impact actions.

This is a portfolio-grade platform that runs on synthetic data. It is advisory
only and never issues turbine commands.

## Status

The repository structure is in place. No services are implemented yet.
Development starts with Phase 1 of the delivery plan.

## Requirements

[docs/srs.md](docs/srs.md) is the repository copy of SRS v1.1, the source of
truth for scope and architecture. Section 12 sets the delivery order.

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
