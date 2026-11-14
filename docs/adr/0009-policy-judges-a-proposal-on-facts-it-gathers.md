# ADR 0009: Policy judges a proposal on facts it gathers itself, and fails closed

**Status:** Accepted, 2026-11-14

## Context

The Policy Service (M6.1) evaluates risk, safety, weather-window and escalation rules "on a recommendation" and
returns pass, block or escalate with the rule versions and inputs used (FR-POL-002, FR-POL-003). If a required input
is missing or stale, the result must be block or escalate (FR-POL-005). Recommendations only arrive with the agents in
Phase 8, so the shape of what is judged has to be settled now. So does a harder question: where the inputs come from.
If a caller could pass in the case's severity, the evidence's age or the forecast wind, an agent could talk a proposal
past the very rules meant to stop it.

## Decision

**What is judged.** A *proposal*: the action recommended on a case. It has:
- a kind: inspection, repair, replacement, adjustment or monitoring;
- whether it needs work at height;
- a planned window;
- an estimated cost, downtime and confidence.

A recommendation and a draft work order will carry one. Only the proposal comes from the caller.

**Where the facts come from.** The Policy Service gathers every other input as itself, with fixed read-only grants
([ADR 0008](0008-services-act-as-themselves-with-fixed-read-grants.md)):

| Input | From |
| --- | --- |
| The case's status and severity, and its evidence's status and age | Investigation |
| The turbine's status and its site, the wind farm | Asset |
| The forecast wind and gusts over the planned window | Weather, from M6.3; FR-POL-004 is M6.4 |

The caller's own permission is checked separately: they must be able to read the case's turbine
(`investigation.read`), or the case is not found. The service's grants are `turbine.read` and `investigation.read`.
M6.4 adds `weather.read`.

**Rules are data.** Each rule names one input and the limit it must not cross:
- `limit`: a number above or below a limit, with an override per site (FR-POL-001);
- `match`: a value in, or not in, a list.

It may apply only when another input holds, such as only to work at height. Crossing the limit gives the rule's
outcome, block or escalate. Rules are versioned, hashed and audited as fault rules are. Changing one needs
`policy.manage` and a recent second factor.

**Failing closed is structural.** Every rule states `when_missing`, and the service refuses a definition whose
`when_missing` is anything but block or escalate. So no rule can be written to pass on an input it does not have.
- **An input counts as missing** when it is missing, stale (older than the rule's `max_age_seconds`), of the wrong type,
  or from an owner that cannot be reached.
- **A condition that cannot be read** is assumed to hold, so the rule still applies.
- **A site that cannot be read** gets the strictest of a rule's limits, never a looser site's.
- **An unreachable Investigation Service** stops the evaluation with UNAVAILABLE, since the case is unknown and nothing
  can be judged. Callers treat an absent result as a failure, so no recommendation reaches `pending_review` without
  one (SRS 7.7).

Until the Weather Service exists, every proposal that needs work at height is blocked because the weather is
unavailable. That is FR-POL-005 working as written, not a gap.

**Results never change.** Each evaluation is recorded as a result, a `policy_result_id`, which holds:
- the proposal;
- every input with its source and source record;
- every rule's version, content hash, outcome, the limit applied and where that limit came from;
- the worst outcome.

It is hashed over its canonical JSON, and a database trigger refuses to change or delete it. Reading a result
checks its hash again. Investigation results reference it by ID (SRS 6).

## Consequences

- No caller, person or agent can supply the facts a proposal is judged on, only the proposal itself.
- A proposal that needs work at height cannot pass until M6.3 and M6.4 bring a structured forecast.
- **The default library, `policy-rules-1`, holds starting values, not a site's approved figures.** Each tenant sets its
  own, and each site's, as new versions:
  - climbing limits of 12 m/s wind and 18 m/s gusts;
  - evidence at most a day old;
  - escalation at a critical case, confidence below 0.6, cost above €50,000 or downtime above 48 hours.
- Every evaluation is a stored record, so a proposal judged twice is two results, unless the same idempotency key
  is reused.
