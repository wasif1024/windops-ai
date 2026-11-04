# Coding standards

Everyone working in this repository follows these rules, including AI coding agents.
"Must" is mandatory, and "should" is the default unless a pull request explains why not.
The [SRS](srs.md) decides what to build. This document covers how the work is done.

## How a change reaches `main`

1. Create a branch from the latest `main`.
2. Make the change, with its tests and documents.
3. Run `make check` until it passes.
4. Push the branch and open a draft pull request, which shows as work in progress.
5. Mark it ready for review when the work is finished.
6. The automated checks run on the pull request.
7. Fix anything that fails, and get the change reviewed.
8. Squash and merge once every merge rule below is met.

Nobody pushes directly to `main`, and this includes urgent fixes and document changes.

## Pull request status

Every open pull request shows one status. GitHub's check results and merged state appear
alongside it.

| Status | Meaning | Set by | Can merge |
| --- | --- | --- | --- |
| `status: wip` | Work in progress, and not ready for review | Automatically, on a draft pull request | No |
| `status: ready for review` | Finished and waiting for review | Automatically, when marked ready or when commits are pushed | Yes, once all merge rules are met |
| `status: changes requested` | A reviewer asked for changes | Automatically, from the review | Only after pushing the changes |
| `status: approved` | A reviewer approved it | Automatically, from the review | Yes, once all merge rules are met |
| `status: blocked` | Waiting on something else; say what in a comment | By hand, alongside the other status | No |

A new commit returns an approved pull request to `status: ready for review`.

## Merge rules

A pull request can merge only when all of these are true:

- It is not a draft, and is not marked `status: wip` or `status: blocked`.
- All automated checks pass. A failing check is fixed, not bypassed.
- The change has been tested, and the pull request says how.
- The branch is up to date with `main`.
- Review comments are resolved.
- It has been reviewed. Once the project has a second maintainer, that person must not
  be the author. Until then, the author reviews their own diff against the checklist below.
- Any module it finishes is ticked in the [tracker](phases/README.md).

## What is enforced today

| Rule | Enforced by | Status |
| --- | --- | --- |
| Lint, formatting, types, contracts and tests pass | `check` job in GitHub Actions, and `make check` locally | Active |
| Dependencies have no known vulnerabilities | `dependency-audit` job, and `make audit` locally | Active |
| No secrets in the repository or its history | `secrets` job, and GitHub push protection | Active |
| Static analysis of the code | `codeql` job | Active |
| Status labels, and no merge while WIP or blocked | `pr-status` job | Active |
| Pull request titles follow the commit format | `pr-status` job | Active |
| No direct pushes, and no merge with failing checks | Branch protection on `main` | Active |
| Squash merge only, and merged branches are deleted | Repository settings | Active |
| Contract lint and breaking-change checks | Buf, in the `check` job | Active |
| Container images are scanned | GitHub Actions | Arrives in M1.6 |
| Reviewer differs from author | Branch protection on `main` | When a second maintainer joins |

Until a rule is enforced automatically, the author is responsible for following it.

## Branches

Use `<type>/<short-description>` in lowercase with hyphens. Add the module number when
there is one, as in `feat/m4-1-asset-service` or `fix/telemetry-duplicate-labels`. The
types are the same as for commit messages.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
feat(telemetry): label out-of-order samples

Quality labels were missing for late batches, which hid delayed data
from fault detection.

Refs: M4.6, FR-TEL-003
```

- **Format:** `<type>(<scope>): <summary>`. The scope is optional, and is a service name
  or area such as `telemetry` or `ci`.
- **Summary:** at most 72 characters, in the imperative ("add", not "added"), with no
  ending period.
- **Body:** explains why the change is needed when the summary does not make that clear.
- **Footer:** gives the module and requirement IDs when there are any.

| Type | Use for |
| --- | --- |
| `feat` | New behavior |
| `fix` | A bug fix |
| `docs` | Documents only |
| `test` | Tests only |
| `refactor` | Code changes with no change in behavior |
| `perf` | A performance improvement |
| `build` | Build tools, images and dependencies |
| `ci` | Automated checks and workflows |
| `chore` | Other maintenance |

## Pull requests

- **Title:** follows the commit message format, because it becomes the commit on `main`.
- **Size:** covers one module or one concern. Split anything larger.
- **Description:** states what changed, why, how it was tested, and its module and
  requirement IDs.
- **Merge method:** squash and merge, then delete the branch.

Before asking for review, check that:

- [ ] `make check` passes.
- [ ] New behavior has tests, including failure paths.
- [ ] Documents, and the tracker where relevant, are updated.
- [ ] No secrets, tokens or personal data appear in code, logs or fixtures.
- [ ] Prompt, model, tool or workflow changes passed the regression evaluations.

## Naming

| Item | Convention | Example |
| --- | --- | --- |
| Python modules, functions and variables | `snake_case` | `create_snapshot` |
| Python classes | `PascalCase` | `CircuitBreaker` |
| Python constants | `UPPER_SNAKE_CASE` | `MAX_RETRIES` |
| Test functions | `test_<expected_behavior>` | `test_missing_weather_fails_closed` |
| Service directories | The names in SRS 11.5 | `services/telemetry-service` |
| Workloads and images | `windops-<name>` | `windops-telemetry` |
| Protobuf packages | `windops.<domain>.v<major>` | `windops.telemetry.v1` |
| Protobuf messages and RPCs | `PascalCase` | `CreateTelemetrySnapshot` |
| Protobuf fields | `snake_case` | `turbine_id` |
| Protobuf enum values | `UPPER_SNAKE_CASE`, prefixed with the type | `METRIC_VIBRATION_RMS` |
| Event subjects | Lowercase `<domain>.<past_tense>` | `fault.detected` |
| Permissions | Lowercase `<resource>.<action>` | `maintenance.approve` |
| Database tables and columns | `snake_case`, with plural table names | `work_orders.tenant_id` |
| Environment variables | `UPPER_SNAKE_CASE` with a `WINDOPS_` prefix | `WINDOPS_LOG_LEVEL` |
| TypeScript variables and functions | `camelCase` | `fetchAlarms` |
| React components and their files | `PascalCase` | `AlarmConsole.tsx` |
| Decision records | `NNNN-short-title.md` | `0001-local-minikube-profile.md` |

Use full words over abbreviations, except for established terms such as `gRPC` and `SCADA`.

## Python

- Target Python 3.13. Manage packages with `uv`, and commit `uv.lock`.
- Add type hints to all code. It must pass strict mypy, and `# type: ignore` needs a reason.
- Ruff decides formatting and lint rules, so style is not debated in review.
- Keep functions small and focused. Comments explain why, not what.
- Do not catch an exception unless you handle it. Never hide a failure.

## Service rules

These come from the SRS, and reviewers check them on every change:

- A service must not read or write another service's tables (ARC-002).
- Shared code holds contracts and cross-cutting libraries, never business logic (ARC-004).
- Service-to-service calls use gRPC, with deadlines and trace context (GRP-001, GRP-003).
  [gRPC contracts](grpc-contracts.md) has the contract rules.
- State-changing calls accept an idempotency key (GRP-007).
- Remote calls go through the shared resilience library (SRS 7.7).
- Configuration comes from ConfigMaps and Secrets, never from an image (DEP-003).
- Tenant identity comes from verified metadata, never from a request field (GRP-004, RBAC-001).
- Logs use the shared library, with no secrets or unnecessary personal data (OBS-001, OBS-003).

## AI and agent rules

- An LLM must not own thresholds, severity, financial figures or approvals (SRS 2.1).
- Agents can reach only the draft state, and can never approve (FR-MNT-003, FR-APR-001).
- Agent tools go through allowlisted MCP servers (MCP-002).
- Retrieved content is untrusted data and cannot change a plan or permission (MCP-005, MCP-007).
- Model IDs and effort are versioned configuration, never hard-coded in prompts (LLM-001).

## Testing

- New behavior ships with tests, and a bug fix ships with a test that fails without it.
- Tests must be deterministic, and must not call live external services unless they belong
  to the scheduled live suite (EVL-001).
- Test the failure paths too: denied access, missing input and open circuit breakers.
- Each service keeps its own tests, and `tests/` holds cross-service tests (ARC-001).

## Security

- Never commit secrets. Local secrets stay in ignored files (SRS 9.5).
- Pin dependencies, and add one only when it is needed.
- Treat all external input as untrusted, and validate it where it enters the system.

## Changing these standards

Change this document through a normal pull request. A significant decision also gets a
record in [docs/adr/](adr/).
