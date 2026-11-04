# ADR 0003: Store telemetry in PostgreSQL, without TimescaleDB

**Status:** Accepted, 2026-11-05

## Context

SRS v1.1 stored telemetry in TimescaleDB, for its partitioning, continuous summaries, compression
and retention. We reviewed what it adds before building M2.1 and decided not to use it. Plain
PostgreSQL can meet the SRS 8.3 rates and retention with more of our own code, and it avoids the
extension's non-open-source license and its tie to the Telemetry Service's queries and migrations.

## Decision

- Use the official PostgreSQL image with no time-series extension.
- Store telemetry in native range partitions on event time.
- Have the Telemetry Service create partitions and write summaries, and recalculate a summary when
  late data arrives for its period.
- Apply retention by dropping whole partitions.
- Run one PostgreSQL server, with a separate database and login per service (ARC-003), to save memory
  on the local profile.
- Keep Redis as the cache, as SRS 8.1 already says. It holds no source telemetry.

## Production design

The shared server suits a laptop, but if it fails, every service loses its database until it restarts.
Separate pods would cost up to 10 GiB locally, and still would not survive the machine failing. So the
local cluster keeps one server, and a production deployment would add:

- A standby on another machine for each server, which takes over automatically.
- Scheduled backups, tested by restoring them (M14.3).
- Dedicated servers for Telemetry, the busiest service, and Audit, the most critical record. Other
  services share one.

This is a design only, and it is not built. Separate databases make a move mostly a change of host and
credentials. Running a database inside a service's pod was rejected, because each replica would get its
own separate data, and every rolling update would restart the database.

## Consequences

- One less license and dependency to manage, and standard SQL throughout.
- M4.5 grows, because the Telemetry Service owns partition and summary code that needs its own tests,
  including late data.
- Telemetry is stored uncompressed, so it uses more disk. Phase 13 records the measured figure.
- A much larger fleet may need this decision revisited. M13.5 load tests provide the evidence.
- All services share one server, so one heavy service can slow the others. M13.1 adds limits per
  dependency.
