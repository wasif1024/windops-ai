# Implementation tracker

This is the single record of what has been built and what comes next. It breaks
the 15 phases in [SRS v1.1 section 12](../srs.md#12-delivery-phases) into modules, with Phase 4A added
between Phases 4 and 5 by [ADR 0006](../adr/0006-azure-aligned-platform-services.md).
The SRS defines the requirements, and this file tracks progress against them.

**Current position:** Phase 4A, Azure alignment ([ADR 0006](../adr/0006-azure-aligned-platform-services.md)). Six of seven modules are done. M4A.6 is written and waits for one approved pipeline run, after the phase gates run again locally. M4.7 waits until Phase 4A is done.

## How to use this tracker

- **Module:** one deliverable piece of a phase, built and tested on its own.
- **Starting a module:** build it on its own branch and pull request.
- **Finishing a module:** a module is done when its code, tests and documents are
  merged. In the same pull request, tick its box, add the date and pull request
  number, and update **Current position** and the progress table.

  ```text
  - [x] **M1.1 Repository tooling:** ... Done 2026-11-10, PR #4.
  ```

- **Finishing a phase:** the last module of every phase is its exit gate. It can
  be ticked only when the gate passes on a working Minikube deployment, with the
  evidence linked. Phases run in order, and the next one starts after that.
- **Architecture diagram:** after finishing a module, run
  `uv run python -m scripts.generate.readme_visuals` to update the README's progress strip. A test
  fails when a security control it shows as planned has finished, until the control is updated.
- **Plan changes:** add, split or reorder modules here when needed. A change to
  the design also updates the SRS and adds a record in [docs/adr/](../adr/).
- **Throughout:** frontend work, logging, tracing and security tests grow in every
  phase, so each module includes its own share of them.

## Progress

| Phase | Name | Modules | Status |
| --- | --- | --- | --- |
| - | [Prerequisites](#prerequisites) | 3 | Done |
| 1 | [Platform foundation](#phase-1-platform-foundation) | 11 | Done |
| 2 | [Data and events](#phase-2-data-and-events) | 9 | Done |
| 3 | [Identity and tenancy](#phase-3-identity-and-tenancy) | 12 | Done |
| 4 | [Assets and telemetry](#phase-4-assets-and-telemetry) | 10 | In progress |
| 4A | [Azure alignment](#phase-4a-azure-alignment) | 7 | In progress |
| 5 | [Alarms, detection and cases](#phase-5-alarms-detection-and-cases) | 7 | Not started |
| 6 | [Deterministic decisions](#phase-6-deterministic-decisions) | 5 | Not started |
| 7 | [Knowledge and external intelligence](#phase-7-knowledge-and-external-intelligence) | 7 | Not started |
| 8 | [Agent workflow](#phase-8-agent-workflow) | 13 | Not started |
| 9 | [Maintenance and inventory](#phase-9-maintenance-and-inventory) | 5 | Not started |
| 10 | [Approval and closure](#phase-10-approval-and-closure) | 7 | Not started |
| 11 | [Frontend completion](#phase-11-frontend-completion) | 8 | Not started |
| 12 | [Full evaluation](#phase-12-full-evaluation) | 10 | Not started |
| 13 | [Resilience and scale](#phase-13-resilience-and-scale) | 8 | Not started |
| 14 | [Hardening and release](#phase-14-hardening-and-release) | 7 | Not started |
| 15 | [Azure deployment](#phase-15-azure-deployment) | 10 | Not started |

## Optional decisions

| Decision | Status | When |
| --- | --- | --- |
| Add Temporal for the long case process across services | Optional, and not planned. LangGraph checkpoints, JetStream and the outbox cover restarts | Review in Phase 14, using what we learned |

## Prerequisites

- [x] **P.1 SRS baseline:** SRS v1.1 is the complete baseline, so no v1.0 copy is needed. Done 2026-11-04.
- [x] **P.2 SRS diagrams:** copy the roadmap, work-order lifecycle and architecture diagrams from the live SRS into the repository. Done 2026-11-04.
- [x] **P.3 Local capacity:** use a `local` Minikube profile of 6 CPUs and 10 GB ([ADR 0001](../adr/0001-local-minikube-profile.md)). Done 2026-11-04.

## Phase 1: Platform foundation

- [x] **M1.1 Repository tooling:** Python toolchain, pinned dependencies, lint, format and type checks, a `Makefile` with the SRS 11.4 targets, and `docs/coding-standards.md`. Done 2026-11-04, PR #1.
- [x] **M1.2 CI pipeline:** GitHub Actions on every pull request running `make check`, plus dependency, static-analysis and secret scanning (SRS 9.5), pull request status labels with a WIP merge block, and `main` branch protection that requires pull requests and passing checks. Done 2026-11-04, PR #2.
- [x] **M1.3 Contracts:** Buf lint and breaking-change checks, a sample versioned package, and `docs/grpc-contracts.md` (GRP-001, GRP-002, GRP-007, GRP-008). Done 2026-11-04, PR #3.
- [x] **M1.4 Resilience library:** `shared/resilience` with the SRS 7.7 breaker defaults, retries and deadlines (RES-001, RES-003, RES-004, RES-006). Done 2026-11-04, PR #4.
- [x] **M1.5 Observability library:** JSON logs, OpenTelemetry traces and metrics, including circuit breaker state metrics, with secret redaction (OBS-001, OBS-002, OBS-003). Done 2026-11-04, PR #7.
- [x] **M1.6 Service scaffold:** template that creates each service's tests, Dockerfile, health checks, breaker wiring, manifests and README, with image scanning added to CI (SRS 9.5), gRPC deadlines, trace context, verified identity, health and error mapping, and startup, shutdown and health-change logs (GRP-003, GRP-004, GRP-006, ARC-001, ARC-004, ARC-005, DEP-001, DEP-002, DEP-003, DEP-004, DEP-005). Done 2026-11-04, PR #8.
- [x] **M1.7 Minikube and Kustomize:** namespaces, Calico and overlays for `local`, `ci` and a production-style example, with per-workload memory limits and a first recorded memory measurement (DEP-007, K8S-006, K8S-007). Done 2026-11-04, PR #9.
- [x] **M1.8 API Gateway:** APISIX as the only ingress, with etcd and upstream health (SRS 7.2). Done 2026-11-04, PR #10.
- [x] **M1.9 Observability stack:** OpenTelemetry Collector, Prometheus, Alertmanager, Grafana, Loki and Fluent Bit, with the Platform Health and Microservice Health dashboards, and log retention, access and rotation per environment (SRS 10.1, 10.3; OBS-004). Done 2026-11-04, PR #12.
- [x] **M1.10 Sample services:** two scaffolded services joined by a gRPC call, plus `docs/architecture.md`, `docs/microservices.md` and `docs/deployment.md`. Done 2026-11-04, PR #14.
- [x] **M1.11 Exit gate:** a sample gRPC call is traced through the gateway, and its breaker opens under an injected fault. Done 2026-11-04, PR #15, [evidence](phase-01-exit-gate.md).

## Phase 2: Data and events

- [x] **M2.1 PostgreSQL:** persistent storage, with a separate database or schema and credentials per service (ARC-002, ARC-003, DEP-006). Done 2026-11-05, PR #16.
- [x] **M2.2 Redis:** cache and rate-limit use only, with TTLs and tenant-scoped keys (DAT-004, DAT-007, EVT-005). Done 2026-11-05, PR #20.
- [x] **M2.3 Qdrant:** deployed with persistent storage (SRS 8.1). Done 2026-11-05, PR #21.
- [x] **M2.4 JetStream:** streams, the SRS 7.6 subjects, Protobuf events, tenant identifiers and a dead-letter stream (EVT-001, EVT-003). Done 2026-11-05, PR #22.
- [x] **M2.5 Alembic Jobs:** per-service expand/contract migrations, run once per release as Jobs (DAT-001, DAT-002, K8S-005). Done 2026-11-05, PR #23.
- [x] **M2.6 Outbox library:** state changes and events committed together (EVT-002). Done 2026-11-05, PR #24.
- [x] **M2.7 Audit Service:** append-only, hash-chained records with durable events (FR-AUD-001, FR-AUD-002, EVT-004). Done 2026-11-05, PR #26.
- [x] **M2.8 Events visibility:** Events dashboard, data-store health panels and `docs/events.md`. Done 2026-11-05, PR #28.
- [x] **M2.9 Exit gate:** an event survives a pod restart, and a redelivered audit event creates only one record. Done 2026-11-05, PR #29, [evidence](phase-02-exit-gate.md).

## Phase 3: Identity and tenancy

- [x] **M3.1 Keycloak:** realm, Organizations, OIDC, tenant claim, password policy and `make identity-seed`, and confirm the tenant model vendor check (SEC-001, SEC-004, SEC-005). Done 2026-11-05, PR #31.
- [x] **M3.2 MFA and step-up:** TOTP or WebAuthn, recovery codes and step-up authentication (SEC-002, SEC-003). Done 2026-11-05, PR #32.
- [x] **M3.3 Service identity:** cert-manager and mTLS everywhere, with signed internal identity in `shared/auth` (GRP-005, K8S-002).
- [x] **M3.5 Authorization Service:** deny-by-default decisions, the eight roles and the SRS 3.2 matrix, scoped grants and break-glass access (RBAC-001, RBAC-002, RBAC-003, SEC-006).
- [x] **M3.4 Identity Service:** user lifecycle, sessions and MFA status around Keycloak (SRS 7.2). Token exchange moved to M3.7, where Keycloak gets its fixed address.
- [x] **M3.6 Tenant Service:** tenant lifecycle and branding, with row-level security and cross-tenant tests (FR-TEN-001, FR-TEN-002, DAT-005, DAT-008, SEC-007).
- [x] **M3.7 Web BFF and edge policy:** REST, OpenAPI, gateway TLS, CORS allowlists and the SRS 7.5 rate limits, plus Keycloak's fixed address and token exchange (API-001, API-002, API-005, SEC-008).
- [x] **M3.8 Frontend shell:** React and TypeScript in its own image, with login, role-aware navigation and tenant branding (SRS 4.11).
- [x] **M3.9 Base administrator console:** users, roles, permissions, MFA status, sessions and access denials (SRS 9.4). Done 2026-11-06, PRs #47, #48 and this one.
- [x] **M3.10 Platform console:** the Platform Administrator's screens across tenants: list, create and suspend tenants, their branding and domains, and break-glass access, with the Web BFF routes they need (FR-TEN-001, RBAC-003). Added 2026-11-06. Branding stays the tenant's own to change, so the Platform console shows it read-only, and tenant administrators got a Branding tab. Done 2026-11-06, PRs #50 and this one.
- [x] **M3.11 Security visibility:** Security dashboard, plus `docs/security.md` and `docs/rbac.md`. Done 2026-11-07, with the SRS 10.3 security alerts and `make security-check`.
- [x] **M3.12 Exit gate:** cross-tenant and missing-permission tests all fail safely, and administrator MFA is enforced. Done 2026-11-07: `make phase-3-gate` runs a sweep of every Web BFF route and all 17 Phase 3 checks.

## Phase 4: Assets and telemetry

- [x] **M4.1 Asset Service:** wind farms, turbines, coordinates, status and component hierarchy (FR-AST-001, FR-AST-002). Done 2026-11-07, with synthetic demo assets (`make asset-seed`).
- [x] **M4.2 Simulator core:** seeded, versioned physical models, with all data marked synthetic (SIM-001, SIM-003, SIM-006). Done 2026-11-07, as the `windops-simulator` package. It streams live from its own workload since M4.4.
- [x] **M4.3 Fault scenarios:** the six starting profiles, injected data problems and hidden ground truth (SIM-002, SIM-004, SIM-005). Done 2026-11-07, with seven named scenarios. The ground truth goes to a file for now, as the Evaluation Service arrives in Phase 12.
- [x] **M4.4 Telemetry ingestion:** authenticated device route and required metadata (FR-TEL-001, FR-TEL-002, FR-TEL-005, API-004). Done 2026-11-08: the Telemetry Service, device credentials and `/ingest/*` (`make telemetry-check`), and the simulator streaming the demo fleet live from its own workload (`make simulator-check`).
- [x] **M4.5 Telemetry storage:** PostgreSQL time partitions and summaries, with the SRS 8.3 rates and retention (FR-TEL-004). Done 2026-11-08: 10-minute SCADA summaries, recalculated on late data, and retention by whole partitions with snapshot holds (`make telemetry-storage-check`), and CMS vibration batches with spectral band features and scheduled or triggered raw windows (`make cms-check`).
- [x] **M4.6 Data-quality detection:** label missing, duplicated, delayed, malformed and out-of-order data (FR-TEL-003). Done 2026-11-09: every sample labelled `ok`, `duplicate`, `delayed` or `out_of_order` as its period is summarized, gaps kept per period, and readable malformed samples kept by sequence, matched to the simulator's ground truth in a test (`make data-quality-check`).
- [ ] **M4.7 Telemetry snapshots:** the SRS 7.4 streaming and snapshot contract, with reproducible metadata (FR-TEL-006).
- [ ] **M4.8 Fleet screens:** fleet overview, turbine detail and telemetry charts (SRS 4.11).
- [ ] **M4.9 Freshness visibility:** Data Freshness dashboard, starting with telemetry delay (SRS 10.3).
- [ ] **M4.10 Exit gate:** a seeded fault profile streams into storage with the correct quality labels.

## Phase 4A: Azure alignment

Added by [ADR 0006](../adr/0006-azure-aligned-platform-services.md), before M4.7, so the system follows Azure's
[advanced AKS microservices architecture](https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks-microservices/aks-microservices-advanced).
`WINDOPS_ENVIRONMENT` chooses each service's back end: `local` keeps today's, and `dev`, `staging` or `live` uses Azure's. Stage
1, the code and its local proof, costs nothing. Stage 2, M4A.6 and the live tests, creates Azure resources only after
their plan and monthly price are approved.

- [x] **M4A.1 Azure alignment decision:** ADR 0006, this phase, the SRS and the settings. Done 2026-11-09.
- [x] **M4A.2 Cilium:** the local cluster on Cilium in place of Calico. Done 2026-11-11: `make cluster` uses Cilium, and `make cluster-check` proves Cilium is the network plugin and enforces both a Kubernetes NetworkPolicy and a CiliumNetworkPolicy. The phase gates run again at the end of Phase 4A.
- [x] **M4A.3 Event back ends:** one events interface, with NATS JetStream for `local` and Azure Service Bus for `dev` and `live`, the outbox unchanged, one test suite for both, and one run against a real Service Bus namespace. Done 2026-11-11: `open_events()` chooses from `WINDOPS_ENVIRONMENT`, a five-scenario contract runs against both, and a real Standard namespace passed it (`make servicebus-check`) before being deleted.
- [x] **M4A.4 Workload identity:** Microsoft Entra Workload ID for Service Bus, Azure Database for PostgreSQL and Key Vault, with no stored password, and passwords as today locally. Done 2026-11-11: services sign in to PostgreSQL with an Entra token when `WINDOPS_DATABASE_AUTH` chooses it, and the generated `azure` overlay gives every workload its own identity and its secrets from Key Vault (`make azure-overlay`). The migration Jobs, made by a script, follow in M4A.6 with the deployment that runs them.
- [x] **M4A.5 Azure Monitor:** OpenTelemetry to Application Insights and Log Analytics, metrics to managed Prometheus, and the dashboards and alert rules as code for Azure Managed Grafana. Done 2026-11-11: `make azure-monitoring` makes the Collector for Application Insights, managed Prometheus's scrape job, Azure Prometheus rule groups with Service Bus alerts in place of JetStream's, and the dashboards for Managed Grafana, all from the local stack and held to it by a test.
- [ ] **M4A.6 Azure foundation and live proof:** the Azure resources in Bicep, created from an approved plan and price, with Service Bus, Workload ID and monitoring proven on them. The plan and price are in [the Azure deployment plan](../azure-plan.md), awaiting approval: about $85 a month for demos, and $20 when idle. In progress: the Bicep and `make azure-plan`, `azure-up`, `azure-down` and `azure-destroy` are written, and a `what-if` of all 93 resources passes. An Azure DevOps pipeline deploys the `staging` environment after an approval, proves it, and removes the compute (docs/azure-devops.md); Azure DevOps is set up. `make azure-platform` deploys the services that need only Azure's services (asset, audit, authorization, tenant and the sample pair): images in the registry, settings in Key Vault, cert-manager with the CA from Key Vault, the Collector for Application Insights, each service's Entra database login and migrations. `make azure-prove` checks Workload ID to PostgreSQL with no password, Key Vault, mutual TLS and traces in Application Insights, and a nightly pipeline stops PostgreSQL. Next: one approved pipeline run, at the end of Phase 4A, proves it on Azure.
- [x] **M4A.7 Azure presentation:** the README and its diagram show the Azure architecture, and the local profile beside it. Done 2026-11-12: the README's On Azure section shows `docs/assets/azure-architecture.png`, made by `scripts/generate/readme_visuals.py`: the delivery pipeline, both resource groups, AKS with what the pipeline deploys and what Phase 15 adds, how a pod signs in with no secret, each concern locally and on Azure, and the cost. Tests hold it to the pipeline's stages and to what `make azure-platform` deploys.

## Phase 5: Alarms, detection and cases

- [ ] **M5.1 Alarm Service:** ingestion, states, acknowledgement and change events, with no LLM severity (FR-ALM-001, FR-ALM-002, FR-ALM-003, FR-ALM-004).
- [ ] **M5.2 Fault rules:** versioned, audited thresholds, statistics and correlation (FR-FLT-001, FR-FLT-002, FR-FLT-005).
- [ ] **M5.3 Detection records:** rule version and telemetry snapshot behind every detection, and `fault.detected` events (FR-FLT-003).
- [ ] **M5.4 Investigation Service:** case lifecycle, deduplication and investigation start (FR-FLT-004, FR-INV-001).
- [ ] **M5.5 Evidence snapshots:** immutable, hashed manifests, with retention exemption for referenced data (FR-INV-002, EVD-001, EVD-002, EVD-003, EVD-004, EVD-005, EVD-006).
- [ ] **M5.6 Alarm screens:** alarm console, acknowledgement and history (SRS 4.11).
- [ ] **M5.7 Exit gate:** a fault creates one deduplicated case bound to an immutable snapshot.

## Phase 6: Deterministic decisions

- [ ] **M6.1 Policy Service:** versioned thresholds, and pass, block or escalate results that fail closed (FR-POL-001, FR-POL-002, FR-POL-003, FR-POL-005).
- [ ] **M6.2 Production Impact Service:** energy, availability and financial calculations with versioned inputs (FR-IMP-001, FR-IMP-002, FR-IMP-003).
- [ ] **M6.3 Weather Service:** Open-Meteo adapter, provenance and freshness, and confirm the forecast provider vendor check (WTH-001, WTH-002, WTH-003, WTH-004, WTH-005).
- [ ] **M6.4 Weather in policy:** safety rules accept weather only from the structured provider (FR-POL-004).
- [ ] **M6.5 Exit gate:** boundary tests pass 100%, and a missing weather input fails closed.

## Phase 7: Knowledge and external intelligence

- [ ] **M7.1 Synthetic documents:** synthetic OEM manuals, bulletins and procedures (SIM-007).
- [ ] **M7.2 Knowledge ingestion:** upload controls, metadata extraction and the embedding adapter, and confirm the embedding vendor check (FR-KNW-001, FR-KNW-002, LLM-011).
- [ ] **M7.3 Retrieval:** citable passages, tenant filters, permission checks and re-indexing (FR-KNW-003, FR-KNW-004, FR-KNW-005, DAT-006).
- [ ] **M7.4 External Intelligence Service:** Tavily, source validation, quotas, and redacted and logged egress (EXT-001, EXT-002, EXT-003, EXT-004, EXT-005, EXT-006, EGR-004).
- [ ] **M7.5 MCP foundation:** authenticated clients, schema validation, limits, audit logs and untrusted-content wrapping (MCP-001, MCP-003, MCP-004, MCP-005).
- [ ] **M7.6 First MCP servers:** Documentation, Tavily and Weather, plus development-only Filesystem and PostgreSQL, and `docs/mcp-integrations.md` (SRS 6.3).
- [ ] **M7.7 Exit gate:** results carry citations and provenance, and permissions are enforced before return.

## Phase 8: Agent workflow

- [ ] **M8.1 Vendor checks:** confirm Claude features through Deep Agents, Claude data handling and LangSmith Sandbox availability, as the first task (SRS 13.4; SBX-004).
- [ ] **M8.2 Agent Runtime Service:** queued jobs and worker replicas, so a replica can fail without losing a run (ARC-006, ARC-007).
- [ ] **M8.3 Claude policy:** versioned model and effort settings, response handling, caching and budgets (LLM-001, LLM-002, LLM-003, LLM-004, LLM-005, LLM-006, LLM-007, LLM-008, LLM-009, LLM-010).
- [ ] **M8.4 LangGraph workflow:** SRS 5.4 steps 1 to 13, checkpoints, terminal states, and pause and resume, with a test that kills a worker mid-run and proves the run resumes from its last checkpoint (AGT-003, AGT-004, AGT-005, FR-INV-007).
- [ ] **M8.5 Agent identities:** token exchange and the `fault-investigator` principal, ineligible for approval and visible in audit (SEC-008, SEC-009, SEC-010).
- [ ] **M8.6 WindOps MCP:** tenant-scoped tools and per-agent allowlists, with separate read and write paths for external content (MCP-002, MCP-006, MCP-007).
- [ ] **M8.7 Agents and skills:** Supervisor, SCADA Analyst, Reliability Engineer, Production Impact, Maintenance Planner and Safety Researcher, plus the six SRS 11.5 skills (FR-IMP-004).
- [ ] **M8.8 Structured results:** the SRS 5.7 schema, citations, contradictions and at most two repairs (FR-INV-003, FR-INV-004, FR-INV-005).
- [ ] **M8.9 LangSmith tracing:** complete sanitized traces, with a redaction library and leakage tests (OBS-005, OBS-006, OBS-007, OBS-008, EGR-003).
- [ ] **M8.10 Agent Sandbox Adapter:** registered task types and limits, with no credentials or default network (SBX-001, SBX-002, SBX-003).
- [ ] **M8.11 Live progress:** case ID returned at once, then resumable server-sent events through Web BFF (API-003).
- [ ] **M8.12 First regression set:** initial scenarios, plus the Agent Operations and LLM Operations dashboards (SRS 10.3).
- [ ] **M8.13 Exit gate:** a simulated fault produces a validated, cited report with a full trace.

## Phase 9: Maintenance and inventory

- [ ] **M9.1 Maintenance Service:** plans, validation and draft work orders, with agents limited to drafts (FR-MNT-002, FR-MNT-003).
- [ ] **M9.2 Inventory Service:** parts, compatibility and stock, with reservations reserved for authorized users or workflows (FR-INVT-001, FR-INVT-002, FR-INVT-003).
- [ ] **M9.3 Planner integration:** the Maintenance Planner agent prepares a proposal and draft, covering SRS 5.4 step 12 (FR-MNT-001).
- [ ] **M9.4 Planner screens:** maintenance planner and parts lookup (SRS 4.11).
- [ ] **M9.5 Exit gate:** an agent can create a draft, and all attempts to submit or reserve as an agent are denied.

## Phase 10: Approval and closure

- [ ] **M10.1 Approval Service:** decision records, no agent approvals, and rechecks that fail closed (FR-APR-001, FR-APR-002, FR-APR-003, EVD-009, EVD-010).
- [ ] **M10.2 Separation of duties:** requester and eligibility rules at decision time (RBAC-004, RBAC-005, RBAC-006, RBAC-007, RBAC-008).
- [ ] **M10.3 Stale evidence:** the SRS 8.5 versioned rules, with new snapshot and proposal versions (EVD-007, EVD-008).
- [ ] **M10.4 Workflow resume:** resume from a durable event after the human decision, covering SRS 5.4 steps 13 and 14.
- [ ] **M10.5 Notification Service:** email and in-app delivery status, without sensitive evidence (FR-NOT-001, FR-NOT-002).
- [ ] **M10.6 Post-maintenance verification:** completion records and a comparison reported to a human, covering SRS 5.4 step 15 (FR-MNT-004, FR-MNT-005).
- [ ] **M10.7 Exit gate:** the full flow passes from fault to verification, with 0 approval bypasses.

## Phase 11: Frontend completion

- [ ] **M11.1 Investigation screens:** list, timeline, live progress and findings, with review, challenge and annotation (FR-INV-006).
- [ ] **M11.2 Evidence viewer:** snapshot contents, citations and data quality (SRS 4.11).
- [ ] **M11.3 Impact and weather views:** production impact view, and weather and source freshness panel (SRS 4.11).
- [ ] **M11.4 Approval queue:** eligible decisions, with stale and superseded states shown (SRS 4.11).
- [ ] **M11.5 Knowledge library:** browsing and management within permissions (SRS 4.11).
- [ ] **M11.6 Audit and notifications:** audit explorer and notification center (SRS 4.11).
- [ ] **M11.7 Full administrator console:** approval policies, MCP integrations and gateway policies, with change review (SRS 4.11, 9.4).
- [ ] **M11.8 Exit gate:** role-based end-to-end browser tests cover every SRS 4.11 screen.

## Phase 12: Full evaluation

- [ ] **M12.1 Evaluation Service:** datasets, evaluation jobs, benchmarks and regression orchestration (SRS 7.2).
- [ ] **M12.2 Scenario pipeline:** versioned scenarios, LangSmith sync, ground truth and separate train, validation and held-out sets (EVL-006).
- [ ] **M12.3 Diagnosis datasets:** turbine fault diagnosis, gearbox diagnostics and vibration analysis.
- [ ] **M12.4 Planning datasets:** weather impact, production impact, maintenance planning and spare parts.
- [ ] **M12.5 Governance datasets:** safety escalation, tool selection, RAG grounding, RBAC and security, human approval and failure recovery, with prompt-injection cases for every external-content tool (MCP-008).
- [ ] **M12.6 Recordings and live suite:** recorded Tavily and weather responses, plus a scheduled live suite (EVL-001).
- [ ] **M12.7 Evaluators:** deterministic checks first, then versioned LLM judges (EVL-003, EVL-004).
- [ ] **M12.8 CI gates and reports:** repeated critical runs and versioned results across all evaluation dimensions, plus `make eval` and `docs/evaluation-strategy.md` (EVL-002, EVL-005, EVL-007).
- [ ] **M12.9 Responsible AI visibility:** Responsible AI dashboard (SRS 10.3).
- [ ] **M12.10 Exit gate:** every SRS 10.7 agent-quality gate passes on held-out scenarios.

## Phase 13: Resilience and scale

- [ ] **M13.1 Bulkheads:** a limit for each dependency, so one slow service cannot consume all workers (RES-002).
- [ ] **M13.2 Fallback tests:** automated fault injection for every breaker and fallback in SRS 7.7, plus `make chaos` (RES-005).
- [ ] **M13.3 Network policies:** default-deny per namespace, named egress endpoints and tenant egress switches (K8S-001, EGR-001, EGR-002).
- [ ] **M13.4 Autoscaling:** autoscalers for stateless services, agent workers and ingestion, with gRPC load balancing (K8S-003, K8S-004).
- [ ] **M13.5 Load tests:** telemetry, concurrent investigations, gateway limits and independent service scaling (SRS 10.8).
- [ ] **M13.6 Capacity record:** measured baselines and resource figures that replace the starting Minikube sizes (SRS 11, 11.6).
- [ ] **M13.7 Alert coverage:** every SRS 10.3 alert, routed by severity and owner.
- [ ] **M13.8 Exit gate:** all breaker tests pass, services scale independently, and capacity is documented.

## Phase 14: Hardening and release

- [ ] **M14.1 Security review:** threat model and review, with all findings resolved or recorded (SRS 10.7).
- [ ] **M14.2 Supply chain:** signed images and pinned dependencies (SRS 9.5).
- [ ] **M14.3 Backup and restore:** tested and documented for PostgreSQL and Qdrant (DAT-003).
- [ ] **M14.4 Runbooks:** operating and recovery procedures, with final documents.
- [ ] **M14.5 Repeatable demo:** `make seed-demo`, `make smoke-test` and the remaining SRS 11.4 targets from a clean machine.
- [ ] **M14.6 Final acceptance:** confirm every SRS 13.1 criterion and SRS 10.7 gate.
- [ ] **M14.7 Exit gate:** no critical findings remain, and a clean `make` bootstrap passes the smoke and end-to-end tests.

## Phase 15: Azure deployment

Phase 4A builds the Azure services, Workload ID, monitoring and their Bicep first ([ADR 0006](../adr/0006-azure-aligned-platform-services.md)). This phase deploys the whole system on them, hardens it and presents it.

- [ ] **M15.1 Azure decisions:** subscription, region, budget with an alert, domain, and refreshed prices and quota ([ADR 0005](../adr/0005-azure-deployment-as-the-final-phase.md)).
- [ ] **M15.2 Foundation:** resource group, Container Registry and Key Vault in Bicep, with GitHub OIDC login and no stored Azure password.
- [ ] **M15.3 Image pipeline:** GitHub Actions builds, scans, signs and pushes every image.
- [ ] **M15.4 AKS cluster:** Azure Kubernetes Service in Bicep, with network policy enforced, and an `azure` overlay with real image tags and storage classes.
- [ ] **M15.5 Managed PostgreSQL:** per-service databases and logins, private network access, TLS and backups.
- [ ] **M15.6 Cloud secrets:** Key Vault replaces the local secret files for every workload.
- [ ] **M15.7 WAF and TLS:** Application Gateway WAF v2 in front of APISIX, with a domain and public TLS.
- [ ] **M15.8 Delivery pipeline:** migrations, then a rolling deployment, with a manual approval and a rollback path.
- [ ] **M15.9 Cost and operations:** stop and start commands, cost record and a cloud runbook.
- [ ] **M15.10 Exit gate:** the pipeline deploys from a clean subscription, and the smoke and end-to-end tests pass through the WAF.
