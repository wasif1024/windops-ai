# ADR 0001: Local Minikube profile for a 16 GB laptop

**Status:** Accepted, 2026-11-04

## Context

SRS v1.1 started Minikube at 8 CPUs and 24 GB of memory, and requires that no
feature is dropped from the local deployment. The development machine is an
Apple M4 laptop with 10 CPUs and 16 GB of memory, so that size cannot run on it.

The SRS already treats the size as a starting point, to be replaced with measured
figures in Phase 13.

## Decision

- Add a `local` profile of 6 CPUs and 11 GB, which leaves about 5 GB for macOS and
  development tools.
- Keep 8 CPUs and 24 GB as the recommended full size.
- Run every workload in the `local` profile, with no feature removed (K8S-007).
- Use one replica per workload and set memory requests and limits per workload.
- Leave autoscaling off locally unless a test enables it.

Phase exit gates run on the `local` profile unless a gate needs more capacity.

## Consequences

- Development and most exit gates need no extra hardware or cloud cost.
- The fit is unproven. A rough estimate of 8.5 to 11.5 GB leaves little or no
  margin, so module M1.7 records a first measurement and later phases repeat it
  as workloads are added.
- Local runs can support only one or two concurrent investigations.
- Performance figures from this profile describe this laptop only.
- The Phase 13 scaling gate may need a larger temporary machine. That decision is
  made in Phase 13, using measured figures.
- If memory runs short, tighten per-workload limits or stop workloads a given test
  does not use. A full-system run still needs every workload, so if that cannot
  fit, this decision must be revisited.
- Free disk space may become a limit before memory, with about 56 GB available.
