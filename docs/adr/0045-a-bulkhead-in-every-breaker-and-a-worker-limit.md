# ADR 0045: A bulkhead in every breaker, and a limit on gRPC workers

**Status:** Accepted, 2026-11-28, for M13.1.

## Context

RES-002 asks for a bulkhead limit on each dependency, so one slow service cannot consume all workers. Phase 12's long
runs showed why: when a service slowed under CPU starvation, its callers' requests piled up waiting on it, and calls to
healthy services queued behind them.

Two pieces were missing. Nothing limited how many calls a service could have in flight to one dependency: a breaker
opens only after slow calls finish, by which time they hold their workers for a whole deadline. And nothing bounded the
workers themselves: the asyncio gRPC server accepted any number of calls at once, so "all workers" had no size.

Every service already calls each dependency through one `CircuitBreaker`, and handles `BreakerOpenError` with the
behavior SRS 7.7 defines for that dependency.

## Decision

**Every breaker holds a bulkhead, so every dependency has one.**
- `CircuitBreaker` takes a `BulkheadConfig`, with defaults of 32 calls in flight, 16 callers waiting, and a wait of
  at most 0.5 seconds for a slot. A freed slot passes to the longest waiter.
- A call beyond that is refused at once with `BulkheadFullError`, without reaching the dependency. It is a
  `BreakerOpenError`, so each caller's existing SRS 7.7 fallback applies unchanged.
- A refusal is not the dependency failing, so it does not count toward opening the breaker, and is never retried.
  A half-open probe refused by the bulkhead is given back.
- Time waiting for a slot does not count toward the dependency's slow-call limit.

**The gRPC workers are bounded.**
- `WINDOPS_GRPC_MAX_CONCURRENT_RPCS`, 128 by default, limits the calls a service handles at once. Beyond it, the
  server answers `RESOURCE_EXHAUSTED`, which the caller's breaker counts as an overloaded dependency.
- One dependency can hold at most 48 of the 128 workers, its 32 in flight and 16 waiting, which leaves most of them
  for the rest.

**Refusals are visible.**
- Each refusal adds to `windops_bulkhead_rejections_total`, by dependency, through the `resilience_metrics` hooks
  every service passes to its breakers.
- The first refusal of an episode is logged, not every one.
- `WindopsBulkheadRejecting` warns when a workload has refused calls to a dependency for five minutes, and the
  Microservice health dashboard charts the refusals.

## Consequences

- A slow dependency costs its callers a bounded share of their workers, and its extra callers get the dependency's
  defined fallback at once, not a full deadline later.
- The defaults suit the local cluster's load. A service whose calls to one dependency are known to run higher can
  pass its own `BulkheadConfig`. M13.5's load tests and M13.6's capacity record set the measured figures.
- The database connection pool, 5 connections plus 5 overflow in each service, remains its own bulkhead for
  PostgreSQL, with its own timeout.
- HTTP services, Web BFF and the MCP servers, get the per-dependency bulkheads too, since they call through the same
  breakers. Their incoming concurrency is left to the gateway's rate limits.
