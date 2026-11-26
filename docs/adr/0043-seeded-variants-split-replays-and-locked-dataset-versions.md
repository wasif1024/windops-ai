# ADR 0043: Seeded variants, one split a replay, and dataset versions locked by their hash

**Status:** Accepted, 2026-11-26, for M12.2.

## Context

SRS 10.5 asks for 100 to 200 versioned scenarios, kept in version control and synchronized to LangSmith. EVL-006
keeps train, validation and held-out scenarios separate, so prompts are not tuned to the final test. SIM-005 sends
each scenario's hidden ground truth to the evaluation system, never to the agents. EVL-005 asks results to record the
dataset version they scored.

The first set had twelve scenarios over six simulator replays. Two train and validation scenarios replayed exactly the
telemetry three held-out scenarios replay. Anyone tuning a prompt on them had seen the final test's data. The
simulator has seven scenarios, far short of 100 distinct replays.

## Decision

**A scenario may name a seed of its own: a variant.**
- The simulator scenario is replayed with that seed. The fault, its component and its timing are the same; the noise
  and link trouble are drawn from the new seed.
- That is how the set grows to 100 to 200 without new simulator code.
- A variant's seed may not be another simulator scenario's own, so a seed never means two things.

**Each replay belongs to one split (EVL-006).**
- A replay is a simulator scenario with its seed. Loading a dataset refuses one replayed in two splits, and any two
  scenarios with the same case title.
- Version 1.1 of the first set gives impact-001, weather-001 and grounding-001 variant seeds, so no held-out telemetry
  is replayed in train or validation.

**The simulator's ground truth is exported for each scenario (SIM-005).**
- The simulator's own record of the replay, for its seed, goes into the scenario's definition: the fault, its
  component, severity, onset and when it was detectable.
- The Evaluation Service keeps it, and LangSmith holds it in each example's outputs, never its inputs.
- The data problems are drawn as the telemetry replays, so they stay with the replay.

**A dataset version is locked by its content hash (EVL-005).**
- The manifest keeps each version's hash, worked out exactly as the Evaluation Service does.
- A repository test fails when a scenario changes without a new version. `make scenarios-lock` records a new version's
  hash, and refuses to rewrite an old one.
- `make regression` runs only a locked version, and `make scenarios` checks that the service's hash matches the lock.

**`make scenarios` is the pipeline.** It checks the set, registers the version with the Evaluation Service and
synchronizes it to LangSmith, each scenario an example in its split, and tags LangSmith's dataset with the version.

## Consequences

- **A mistake is caught early:** a leak between splits, or a silent change to a scenario, fails in the repository,
  before anything runs or costs.
- **Results name a version:** a result in the Evaluation Service or LangSmith can be traced to the exact scenarios it
  scored.
- **More replays:** variants mean more distinct replays, each of its own telemetry, so a full run replays more. Nine
  for 1.1, against six before.
- **What a variant cannot do:** a variant cannot change the fault itself. A new kind of fault still needs a new
  simulator scenario.
