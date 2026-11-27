# ADR 0044: An evaluation tenant on recorded providers

**Status:** Accepted, 2026-11-27, for M12.6.

## Context

EVL-001 asks regression runs to use recorded Tavily and weather responses, and a separate scheduled suite to exercise
the live services. MCP-008 asks the security dataset to plant prompt injections in every tool that returns external
content. M12.5 wrote those injections as recordings, but nothing could give a recording to an agent yet, so the
regression skipped them on Claude.

The regression ran in north-sea, a demo tenant. The External Intelligence and Weather services each have one live
provider, for every tenant. So any switch to recordings would have changed what demo users saw, and a crashed run
would have left it switched.

## Decision

**The regression runs in a tenant of its own, `windops-evaluation`.**
- It is seeded the way the demo tenants are: its organization, demo users, roles and tenant record.
- Its library holds the manufacturer's documents for both demo models, which the regression replays, but neither
  site's restricted procedure.
- It also holds the evaluation documents, which carry the injections, and no demo tenant ever does. One of them
  addresses agents quietly enough that upload screening misses it, so the model's own resistance is tested too.

**Its web research and forecasts are recordings, kept by the Evaluation Service.**
- External Intelligence and Weather send the tenants named in `WINDOPS_REPLAY_TENANTS` to `WINDOPS_REPLAY_URL`, and
  every other tenant to the live provider. The choice is made on each call, by the caller's tenant.
- The Evaluation Service answers in Tavily's and Open-Meteo's own shapes, over HTTPS, only to callers with a
  certificate from the cluster's CA. Tavily's key is never sent there.
- With nothing set, every call gets the baseline: plain search results and pages, and a calm forecast from the current
  hour, so it is always fresh.
- `make regression` calls `SetReplay` before each attempt, with its job's lease. That scenario's recording is then
  what the evaluation tenant gets while the lease holds. A lapsed lease or a finished job gives the baseline again.
- A forecast recording's text goes in a field the Weather Service never reads. The case shows that text dropped, as
  the Weather Service keeps only the provider's numbers.

**The recordings are part of the dataset.**
- A scenario's recording is in its registered definition, so the Evaluation Service has it, and changing one needs a
  new dataset version (EVL-005).
- A documentation tool's recording names an evaluation document, and loading refuses one that names any other.

## Consequences

- Demo tenants always see the live sources. A regression is repeatable, and never spends Tavily credits.
- The injection scenarios are judged on Claude, no longer skipped.
- The Evaluation Service now serves recordings to two services, not only Platform Administrators. It serves only
  recorded, synthetic content, and only over mutual TLS.
- A deployment without `WINDOPS_REPLAY_TENANTS` is entirely live. Production should leave it unset, while local and
  staging set it.
- The live sources still need exercising: that is the scheduled live suite, M12.6b.
