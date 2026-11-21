# ADR 0034: Stale evidence, by versioned rules, revised as new versions

**Status:** Accepted, 2026-11-21, for M10.3.

## Context

SRS 8.5 says that new telemetry alone does not make a recommendation stale. The Investigation Service evaluates
versioned rules, and sets `stale` when any of eight triggers fires:
- **Fault severity:** the deterministic severity changes for the case's asset.
- **Alarm:** a new or changed alarm matches the configured correlation set.
- **Condition drift:** a monitored metric leaves the rule's tolerance around its snapshot value.
- **Procedure:** a cited document version is replaced or withdrawn.
- **Asset:** turbine status or a component record materially changes.
- **Inventory:** a proposed part is no longer available.
- **Weather:** the forecast revision breaches the work window's rule, or forecast data expires.
- **Age:** the snapshot exceeds the policy's limit, starting at 24 hours for high-severity cases.

The requirements that follow:
- **EVD-007:** a stale recommendation cannot be approved, and shows which rule fired.
- **EVD-008:** re-evaluation creates a new snapshot and proposal version, leaving earlier versions intact.

Already in place:
- **The Approval Service (ADR 0032):** it rechecks the current work order and case at decision time.
- **Snapshots (M5):** they are versioned per case. A new one is frozen on request (`CreateEvidenceSnapshot`) or when
  detections join.
- **Events announcing changes:** the Asset, Knowledge and Inventory services announce `evidence.changed`, and the
  Alarm Service announces `alarm.changed`.

With the user, on 2026-11-21:
- **Condition drift and weather move to M10.3b,** because both need new periodic polling of the Telemetry and
  Weather services.
- **Re-evaluation is a new snapshot, then a planner's revision.** The agents are not run again.

## Decision

**The rules are one pure module, `stale-rules-1`, in the Investigation Service.** Each rule is judged against the
case's newest snapshot and the proposals submitted on it:

| Rule | Fires when | Its input |
| --- | --- | --- |
| severity | A detection joins and raises the case's severity, once evidence is frozen | `fault.detected`, as it attaches |
| alarm | An alarm on the turbine becomes active at high or critical severity, at a version the snapshot does not hold | `alarm.changed` |
| procedure | A document a submitted proposal cites is replaced | Knowledge's `evidence.changed` |
| asset | The turbine's status, wind farm, model or serial number differs from its frozen record | Asset's `evidence.changed`, then the turbine, read as this service |
| inventory | A proposed part was available and is not, or is retired, incompatible or unknown | Inventory's `evidence.changed`, then `CheckParts`, as this service |
| age | The snapshot is older than 24 hours for critical and high cases, 72 for medium and 168 for low | A sweep every five minutes |

- **The correlation set:** high and critical alarms on the case's turbine.
- **Age limits:** set in the rules until a tenant's policy carries its own.
- **Changing a rule:** any change is a new version name.
- **Permissions:** the Investigation Service gains `inventory.read` among its fixed service grants.
- **Owners that cannot be asked:** the event is retried later. A rule is never judged without its input.

**What the rules watch.** `workorder.submitted` now also carries the proposal's parts, with what Inventory found at
submission, and the procedures it cites. These are new fields only. The Investigation Service keeps the newest
version of each submitted proposal. Drafts are not watched: until submitted they cannot be approved, and their
validation sees a stale case anyway.

**Stale marks the snapshot, once.** The first rule to fire records on the case:
- the rule, what it found, when, and the rules' version;
- a `stale` entry in the case's history;
- `case.updated` with action `stale`, which now also carries the snapshot and the rule.

Later firings change nothing until the case is re-evaluated. The case's status does not change, because staleness
is about its evidence, not its progress.

**Each owner applies it:**
- **Maintenance:** on that `case.updated`, it marks every draft or submitted work order on the stale snapshot as
  `stale`, keeping the rule and what it found.
- **Validation:** a stale case is a blocking problem, so nothing on it is submitted.
- **Approval:** the recheck refuses a stale work order or a stale case and supersedes the request, with the rule in
  its reason (EVD-007). The approval queue and the planner screen both show it.

**Re-evaluation is a new snapshot. Revision is a new version (EVD-008):**
- **Re-evaluating:** freezing a new snapshot clears the case's stale mark. That happens on request, or when a
  detection joins.
- **Revising:** `ReviseWorkOrder` turns a stale or rejected work order into a draft of its next version, on the
  case's current snapshot, validated at once. The earlier version is first kept as it was, in
  `work_order_versions`, which a trigger keeps unchanged, and `GetWorkOrder` returns it.
- **While still stale:** revising is refused while the case itself is stale, so a new version never stands on
  evidence already known to be stale.
- **What follows:** submitted, the new version opens its own approval request.

**Separation of duties counts a revision.** Revising is a change, so the Approval Service's rules become
`separation-of-duties-2`, with `revised` among the actions that make someone a requester (ADR 0033).

## Consequences

- **M10.3b** adds condition drift and weather, each with its own polling and a new rules version.
- **Snapshots that move without a rule:** a new snapshot frozen when a detection of the same severity joins still
  supersedes pending requests, as ADR 0032 says, though no rule fired. That is the approval being made on the
  newest evidence, not a staleness.
- **Proposals submitted earlier:** a proposal submitted before this module has no watched parts or procedures, so
  only the severity, alarm, asset and age rules apply to it.
- **More subscriptions:** each event the rules read is its own durable consumer. On Service Bus that is three more
  subscriptions for the Investigation Service and one for Maintenance, so a service may now own several
  subscriptions.
