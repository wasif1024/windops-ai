# ADR 0039: Drift by the case's fault figures, and weather by Policy

**Status:** Accepted, 2026-11-25, for M10.3b.

## Context

SRS 8.5 has two stale triggers that no event announces, so ADR 0034 left them for polling:
- **Condition drift:** a monitored metric leaves the rule-defined tolerance around its snapshot value.
- **Weather:** the forecast revision breaches the work window's rule, or forecast data expires.

What constrains them:
- **New telemetry alone never makes a recommendation stale** (SRS 8.5).
- **Raw temperatures follow load.** On the demo turbines, a bearing moved about 12 °C in a couple of hours with the
  wind alone, so a tolerance on raw metrics would make cases stale whenever the wind changes. The user chose to watch
  the case's own fault figures instead.
- **The work-window rule lives in the Policy Service** (FR-POL-004), which already blocks work at height above the
  wind and gust limits, and on a forecast older than three hours. Copying those rules here would let them disagree.

## Decision

**A watcher** in the Investigation Service runs every five minutes, as the service, over each case whose newest
snapshot is still fresh, from `open` to `approved`. Once the work is done, the case's verification judges the
turbine instead (ADR 0038). The first rule to fire marks the snapshot stale, as the other rules do. The rules are now
`stale-rules-2`.

**Drift**, from the case's own fault figures:
- **The figure:** each fault rule's `amount`, the figure its severity comes from. For a residual rule, that is the
  temperature above what the load explains, so load alone never moves it.
- **When frozen:** the newest detection of each rule in the snapshot.
- **Now:** Fault Detection's `EvaluateTurbine`, run again at the current moment, which records nothing.
- **The tolerance:** it fires when the figure moves more than half its frozen value, either way, and by at least 1 in
  the rule's units.
- **Which rules:** only threshold, residual and z-score rules have a figure that drifts. Data gaps and alarm
  correlations are left to their own rules.
- **What is not drift:**
  - a rule that no longer finds anything, because a fault that clears is judged at verification;
  - anything about a case a person opened with no detections, which has no monitored figure.

**Weather**, by asking Policy again:
- **What it covers:** each proposal submitted on the snapshot that plans work at height, in a window not yet over.
- **What the event carries:** `workorder.submitted` now names the Policy result from submission, and the impact
  estimate it judged. Investigation keeps both with the proposal.
- **What the watcher does:** it reads that result and asks `EvaluateProposal` again with the same proposal. Only the
  `weather_window` rules count.
- **When it fires:** when one of those rules does worse than it did at submission (pass, then escalate, then block).
  A new work-window rule counts as having passed at submission.
- **Expiry:** an expired forecast blocks in Policy, so it fires too.
- **What it costs:** each asking is a Policy result, kept and audited. The idempotency key covers the proposal, the
  hour and the versions of the work-window rules. So Policy is asked at most once an hour for each proposal, and
  again sooner only when those rules change.

**When an owner is down,** the case is judged at the next pass, never without the answer (SRS 7.7). One case failing
never holds up the others.

## Consequences

- **The check:** `make stale-watch-check` proves both with the real services, in up to 15 minutes.
  - **Drift:**
    - acting as a device, the check sends its own turbine's vibration at 2 mm/s;
    - a check-only rule opens a case, whose evidence freezes 2;
    - the vibration rises to 6 at the same severity, and the case goes stale by drift.
  - **Weather:**
    - work at height is submitted on a demo turbine;
    - a check-only work-window rule that no wind meets is published;
    - the case goes stale by weather, and its work order with it.
- **Within the hour:** a forecast revised within the hour is judged at the next hour. Work planned less than an hour
  ahead is not watched closely enough to rely on, and a planner should check the forecast directly.
- **Unchanged:** Approval's recheck at decision time (EVD-009) is still the last word. The watcher only marks
  staleness earlier, with its reason.
- **Revisit if:**
  - a tenant needs a tolerance of its own;
  - drift should also watch raw metrics for cases a person opened;
  - the weather poll should be more often than hourly.
