# ADR 0042: The Evaluation Service keeps the record and the verdict, and a runner does the runs

**Status:** Accepted, 2026-11-26, for M12.1.

## Context

SRS 7.2 gives the Evaluation Service datasets, evaluation jobs, benchmarks and regression
orchestration. SRS 7.7 says that when it is down, evaluation jobs wait, and a skipped evaluation
is never counted as passed. SRS 10.6 asks for repeated runs of critical cases (EVL-002), results
that record what they depended on (EVL-005), and separate train, validation and held-out splits
(EVL-006).

Since M8.12, `make regression` has done all of it on the host. It replays a simulator scenario
through the gateway's device route, asks Web BFF to investigate as a person would, scores each
run deterministically, and decides pass or fail itself. Nothing on the platform held the result.

A run needs things only the host has today:
- the simulator, to replay a scenario's telemetry;
- a person's sign-in, to investigate through Web BFF as API-003 requires;
- the LangSmith key, to keep scores as an experiment.

Moving all of that into the cluster would make the service a second, privileged client of every
other service.

## Decision

**The service is the record and the judge; a runner does the runs.**

- **Datasets:**
  - A dataset is a named, versioned set of scenarios, with each scenario's family, split,
    severity, runs needed and full definition, ground truth included.
  - A version never changes once registered. Registering it again with the same content does
    nothing, and with other content is refused. A database trigger refuses any change too.
  - Results therefore always name exactly what they scored (EVL-005).
- **Jobs:**
  - A job evaluates one dataset version, for some splits or scenarios, either rehearsed or on
    Claude.
  - Starting it plans every attempt: one a scenario when rehearsed, since a scripted model only
    repeats itself, and each scenario's `runs_required` on Claude (EVL-002).
  - Critical safety, security, approval and tool-scope scenarios must ask for at least five
    runs.
- **Leases:**
  - A runner leases a job, renews the lease, and reports each planned attempt once: its run,
    case, scores, time, tokens and cost, and the versions it read.
  - Only the lease holder may report. When a lease runs out, another runner takes the job over
    and keeps what was reported.
  - A reported attempt never changes, and a trigger holds it so.
- **The verdict:** finishing the job asks the service for the outcome.
  - **Passed:** every planned attempt was reported and passed.
  - **Failed:** every attempt was reported, and at least one failed.
  - **Incomplete:** any attempt is missing, was skipped or could not run. This includes a
    rehearsed attempt in a job on Claude, which says nothing of Claude.

  So a skipped evaluation is never counted as passed (SRS 7.7). A cancelled job is never passed
  either.
- **Benchmarks:**
  - A passed job may be made the benchmark for its dataset, splits and mode.
  - Comparing a later job with it names each scenario as regressed, fixed, the same or new, and
    gives the change in cost and time.
- **Access:**
  - Every call needs `evaluation.manage`, which only a Platform Administrator holds.
  - No agent may call it at all, whatever it was granted: the ground truth must never reach one
    (SIM-005).
  - Its rows belong to the Platform tenant, under the same row-level security as every service.
  - Registrations, starts, leases, outcomes, cancellations and benchmarks are audited through
    its outbox.

**`make regression` is the first runner.**
- It registers the dataset it loaded and starts a job in the mode Investigation runs in.
- It follows the job's plan, not its own count.
- It reports each attempt, including one that could not run, with its reason.
- It always finishes the job, so the service decides even when the run stopped part way.
- It prints the outcome and the comparison with the benchmark, and `BENCHMARK=1` makes a passed
  job the benchmark.
- LangSmith still receives the scores as an experiment, from the runner.

## Consequences

- **The verdict is on the platform:** a gate or a person reads a job's outcome from the service,
  and the runner's own printout is no longer the record.
- **Fail-closed holds:** a runner that stops part way leaves an incomplete job. A service that is
  down fails the runner, never passing anything.
- **Runners are replaceable:** Phase 12's CI gates (M12.8) can run as another runner, in the
  cluster or in CI, against the same API.
- **Repeat runs on Claude cost money:** they are planned by the scenario's `runs_required`. Each
  paid run is still approved first, under the spend rules.
- **Not yet:**
  - The LLM judges (EVL-004) arrive in M12.7, as more scores on the same attempts.
  - The Responsible AI dashboard (M12.9) will read these jobs.
  - Neither changes this API.
