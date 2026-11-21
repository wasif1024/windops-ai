"""When a case's evidence goes stale (SRS 8.5): the versioned rules, pure, so each is testable.

New telemetry alone never makes a recommendation stale. These rules do, each judged against the
case's newest evidence snapshot and the proposals submitted on it:

- severity: a detection joins and raises the case's deterministic severity.
- alarm: after the snapshot was frozen, an alarm on the case's turbine becomes active at high or
  critical severity, at a version the snapshot does not hold.
- procedure: a document a submitted proposal cites is replaced by a newer revision.
- asset: the turbine's status, wind farm, model or serial number differs from the frozen record.
- inventory: a part a submitted proposal names is no longer available: it was available and is
  not, or it is retired, incompatible or unknown.
- age: the snapshot is older than its severity's limit, 24 hours for critical and high, 72 for
  medium and 168 for low.

Condition drift and forecast revisions come in M10.3b. A change to any rule is a new version.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from windops.events.v1 import events_pb2

VERSION = "stale-rules-1"
SEVERITY, ALARM, PROCEDURE, ASSET, INVENTORY, AGE = (
    "severity", "alarm", "procedure", "asset", "inventory", "age",
)  # fmt: skip
# The alarms that matter to a case: those that become active at these severities.
CORRELATED_SEVERITIES = ("high", "critical")
AGE_LIMITS = {
    "critical": timedelta(hours=24),
    "high": timedelta(hours=24),
    "medium": timedelta(hours=72),
    "low": timedelta(hours=168),
}
# The turbine's fields that change what a recommendation is about.
MATERIAL_TURBINE_FIELDS = ("status", "wind_farm_id", "serial_number", "model")
UNAVAILABLE = ("retired", "incompatible", "unknown")


@dataclass(frozen=True)
class Stale:
    rule: str
    detail: str


def severity_raised(before: str, after: str, evidence_version: int) -> Stale | None:
    if evidence_version < 1 or before == after:
        return None
    return Stale(SEVERITY, f"Severity rose from {before} to {after}")


def alarm(
    changed: Any, frozen: Mapping[str, str], changed_at: datetime, frozen_at: datetime
) -> Stale | None:
    """`changed` is an AlarmChanged; `frozen` maps each alarm the snapshot holds to its version.
    A change from before the snapshot was frozen is already judged by it: only a later one counts,
    so an event read again long after changes nothing."""
    if changed_at <= frozen_at:
        return None
    if changed.state != events_pb2.ALARM_STATE_ACTIVE:
        return None
    if changed.severity not in CORRELATED_SEVERITIES:
        return None
    if frozen.get(changed.alarm_id) == str(changed.version):
        return None
    return Stale(ALARM, f"Alarm {changed.code or changed.alarm_id} became active at "
                        f"{changed.severity} severity")  # fmt: skip


def cited_documents(procedures: Iterable[str]) -> set[str]:
    """The documents behind citations such as kn:GBX-OIL-01@3#12."""
    found = set()
    for citation in procedures:
        if citation.startswith("kn:") and "@" in citation:
            found.add(citation[3:].split("@", 1)[0])
    return found


def procedure(document_id: str, replaced_by: str, proposals: Iterable[Any]) -> Stale | None:
    """A document was replaced; `proposals` are those submitted on the snapshot."""
    for proposal in proposals:
        if document_id in cited_documents(proposal.procedures):
            return Stale(PROCEDURE, f"{document_id}, cited by {proposal.work_order_id}, was "
                                    f"replaced by {replaced_by}")  # fmt: skip
    return None


def _material(turbine: Mapping[str, Any]) -> dict[str, Any]:
    return {name: turbine.get(name) for name in MATERIAL_TURBINE_FIELDS}


def asset(frozen: Mapping[str, Any], current: Mapping[str, Any]) -> Stale | None:
    """Both are the turbine's record as a dict, as the snapshot holds it."""
    before, after = _material(frozen), _material(current)
    changed = sorted(name for name in MATERIAL_TURBINE_FIELDS if before[name] != after[name])
    if not changed:
        return None
    if changed == ["status"]:
        return Stale(ASSET, f"The turbine's status changed from {before['status']} to "
                            f"{after['status']}")  # fmt: skip
    return Stale(ASSET, f"The turbine's record changed: {', '.join(changed)}")


def inventory(submitted: Mapping[str, str], now: Mapping[str, str],
              work_order_id: str) -> Stale | None:  # fmt: skip
    """Each part's status at submission and now, by part number."""
    for part, status in sorted(now.items()):
        was = submitted.get(part, "")
        if status in UNAVAILABLE or (was == "available" and status != "available"):
            return Stale(INVENTORY, f"Part {part} for {work_order_id} is now {status}")
    return None


def age(severity: str, frozen_at: datetime, now: datetime) -> Stale | None:
    limit = AGE_LIMITS.get(severity, AGE_LIMITS["high"])
    if now - frozen_at <= limit:
        return None
    hours = int(limit.total_seconds() // 3600)
    return Stale(AGE, f"The evidence is older than {hours} hours, the limit for {severity} cases")
