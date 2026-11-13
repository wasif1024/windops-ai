"""Each turbine's controller, raising and clearing alarms as a real one does (FR-ALM-001).

The controller watches what its sensors report, after any sensor fault, so a drifting sensor
raises the same alarm as a hot gearbox: telling them apart is the investigation's job. Each rule
raises once its condition has held for RAISE_AFTER, and clears once it has stayed below its clear
level for CLEAR_AFTER, so a reading near a threshold does not make the alarm flicker.

The thresholds come from the simulator's own ranges at 1 Hz, as it streams, over a day of the
healthy fleet and each fault scenario. Every threshold sits above the healthy maximum, and the
fault it watches for passes it. A sensor that sends nothing for RAISE_AFTER is lost. The rules are
versioned, and each report says which version raised it. The severity is the controller's own, as
a device sends it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

RULES_VERSION = "controller-alarms-1"
ALARM_SCHEMA_VERSION = "1"
SOURCE = "windops-simulator"
RAISE_AFTER = timedelta(seconds=10)
CLEAR_AFTER = timedelta(seconds=30)


@dataclass(frozen=True)
class Threshold:
    code: str
    reading: str
    raise_at: float
    clear_below: float
    severity: str
    message: str


THRESHOLDS = (
    Threshold("GBX_OIL_TEMP_HIGH", "gearbox_oil_temp_c", 63.0, 60.0, "high",
              "Gearbox oil temperature high"),
    Threshold("GBX_OIL_TEMP_CRITICAL", "gearbox_oil_temp_c", 69.0, 66.0, "critical",
              "Gearbox oil temperature critical"),
    Threshold("GBX_BEARING_TEMP_HIGH", "gearbox_bearing_temp_c", 70.0, 68.0, "high",
              "Gearbox bearing temperature high"),
    Threshold("MAIN_BEARING_TEMP_HIGH", "main_bearing_temp_c", 42.0, 39.0, "high",
              "Main bearing temperature high"),
    Threshold("GEN_BEARING_TEMP_HIGH", "generator_bearing_temp_c", 65.0, 62.0, "high",
              "Generator bearing temperature high"),
    Threshold("NACELLE_VIBRATION_HIGH", "nacelle_vibration_mm_s", 3.0, 2.5, "medium",
              "Nacelle vibration high"),
)  # fmt: skip
SENSOR_LOST = "SENSOR_SIGNAL_LOST"
WATCHED = (
    "wind_speed_mps", "gearbox_oil_temp_c", "gearbox_bearing_temp_c", "main_bearing_temp_c",
    "generator_bearing_temp_c", "nacelle_vibration_mm_s",
)  # fmt: skip
CODES = (*(t.code for t in THRESHOLDS), SENSOR_LOST)


@dataclass
class _Condition:
    """One alarm's state on one turbine: whether it is raised, and since when its sign has held."""

    raised: bool = False
    since: datetime | None = None


@dataclass
class Controller:
    """One turbine's controller. Feed it each sample in order, and send what it reports."""

    tenant_id: str
    turbine_id: str
    _conditions: dict[tuple[str, str], _Condition] = field(default_factory=dict)

    def _report(self, code: str, tag: str, action: str, at: datetime, severity: str,
                message: str) -> dict[str, Any]:  # fmt: skip
        # Unique to this alarm, action and moment, so a resend is recognised as the same report.
        stamp = int(at.timestamp())
        return {
            "schema_version": ALARM_SCHEMA_VERSION,
            "tenant_id": self.tenant_id,
            "turbine_id": self.turbine_id,
            "report_id": f"{code}:{tag}:{action}:{stamp}",
            "code": code,
            "tag": tag,
            "action": action,
            "event_time": at.isoformat(),
            "severity": severity,
            "message": message,
            "source": SOURCE,
            "rules_version": RULES_VERSION,
        }

    def _step(self, key: tuple[str, str], condition: bool, clear: bool, at: datetime,
              severity: str, message: str) -> list[dict[str, Any]]:  # fmt: skip
        """Raise once `condition` has held for RAISE_AFTER; clear once `clear` has held for
        CLEAR_AFTER."""
        state = self._conditions.setdefault(key, _Condition())
        wanted = clear if state.raised else condition
        if not wanted:
            state.since = None
            return []
        state.since = state.since or at
        if at - state.since < (CLEAR_AFTER if state.raised else RAISE_AFTER):
            return []
        state.raised, state.since = not state.raised, None
        code, tag = key
        action = "raise" if state.raised else "clear"
        return [self._report(code, tag, action, at, severity, message)]

    def observe(self, reported: Mapping[str, Any], at: datetime) -> list[dict[str, Any]]:
        """The alarm reports this sample causes: usually none."""
        out: list[dict[str, Any]] = []
        for rule in THRESHOLDS:
            value = reported.get(rule.reading)
            if value is None:
                # A lost reading neither raises nor clears; the lost-sensor alarm says why.
                continue
            out += self._step(
                (rule.code, rule.reading), value >= rule.raise_at, value < rule.clear_below,
                at, rule.severity, rule.message,
            )  # fmt: skip
        for reading in WATCHED:
            lost = reported.get(reading) is None
            out += self._step(
                (SENSOR_LOST, reading), lost, not lost, at, "low",
                f"No signal from {reading.replace('_', ' ')}",
            )  # fmt: skip
        return out

    def standing(self) -> list[tuple[str, str]]:
        """The alarms raised now, as (code, tag)."""
        return sorted(key for key, state in self._conditions.items() if state.raised)
