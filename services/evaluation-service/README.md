# windops-evaluation

The Evaluation Service owns agent evaluation: datasets, evaluation jobs, benchmarks and regression
orchestration (SRS 7.2, SRS 10.5 to 10.7, [ADR 0042](../../docs/adr/0042-the-evaluation-service-keeps-the-record-and-the-verdict.md)).

- **Datasets:** named, versioned sets of scenarios, with each one's family, split, severity, runs
  needed and full definition, ground truth included. A version never changes once registered.
- **Jobs:** one dataset version, some splits or scenarios, rehearsed or on Claude, with every
  attempt planned when it starts: one a scenario rehearsed, each scenario's `runs_required` on
  Claude (EVL-002).
- **Runners** lease a job, report each attempt once, and finish it. Only the lease holder
  reports; a lease that runs out lets another runner take over.
- **The verdict:** passed only when every planned attempt passed; failed when one failed; and
  incomplete when any is missing, skipped, or rehearsed in a job on Claude. A skipped evaluation is
  never counted as passed (SRS 7.7).
- **Benchmarks:** a passed job can be the benchmark for its dataset, splits and mode, and later
  jobs are compared with it.

- **Recorded providers:** the evaluation tenant's web research and forecasts come from here, in Tavily's and
  Open-Meteo's own shapes, over HTTPS for callers with a certificate from the cluster's CA. A runner sets which
  scenario's recording is given while its lease holds; otherwise every call gets the baseline (EVL-001,
  [ADR 0044](../../docs/adr/0044-an-evaluation-tenant-on-recorded-providers.md)).

- **The live suite:** daily, the real Tavily and weather providers through the platform's own services, as the
  service itself, in a tenant whose providers are live. Each run is kept, audited and measured, and alerts say when one
  failed or none has run (EVL-001, M12.6b). `make live-suite` runs it now.

Every call needs `evaluation.manage`, which only a Platform Administrator holds, and no agent may
call it at all (SIM-005). Every change is audited. `make regression` is the first runner.

## Run the tests

```bash
uv run pytest services/evaluation-service/tests
make evaluation-check   # on the cluster: access, datasets, jobs, leases, outcomes, benchmarks
```

## Build the image

```bash
docker build -f services/evaluation-service/Dockerfile -t windops-evaluation:local .
```

## Layout

| File | Holds |
| --- | --- |
| `rules.py` | What the service decides, with no database: plans, attempt results, outcomes, comparisons |
| `replay.py`, `replay_api.py` | The recorded providers' answers, and their HTTPS routes |
| `live.py`, `live_runner.py` | The live suite's checks, and its runs and schedule |
| `service.py` | The gRPC API, under the caller's tenant |
| `store.py` | Its tables and audit records |
| `authorizer.py` | Authorization's `Check`, as the caller; any failure denies |
| `migrations/` | The tables, row-level security, and the triggers that keep datasets and reported attempts unchanged |
