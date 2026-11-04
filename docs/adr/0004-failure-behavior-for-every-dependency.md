# ADR 0004: Define the failure behavior of every dependency

**Status:** Accepted, 2026-11-05

## Context

SRS 7.7 said what happens when a breaker opens for 12 dependencies. It said nothing for 18 others,
including Identity, Keycloak, PostgreSQL, Redis and JetStream. Each service's builder would have had to
decide alone, and M13.2 would have had nothing to test those dependencies against.

## Decision

Add all 18 to SRS 7.7, following the same rules:

- Anything guarding safety or access refuses the action.
- Anything holding evidence pauses or refuses new work, and keeps existing work readable.
- Anything optional lets the work continue, and records the gap or queues the work.

## Consequences

- M13.2 must test 30 behaviors, not 12 (RES-005).
- Protected routes refuse requests during a Redis outage, because rate limits cannot be checked. This
  favors safety over availability, and can be revisited if Redis outages prove disruptive.
- Recommendations wait while Production Impact is down, because no agent may estimate its figures.
