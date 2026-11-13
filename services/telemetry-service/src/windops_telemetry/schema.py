"""What a device sends, at each schema version, and each reading's unit (FR-TEL-001, FR-TEL-002).

A reading's unit is fixed by its name and the schema version, so it is stored once here, not on
every row. The Telemetry Service accepts the simulator's schema version 2 (windops-simulator).
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA_VERSIONS = frozenset({"2"})


@dataclass(frozen=True)
class Reading:
    name: str
    unit: str


# A reading may be null, while its sensor sends nothing.
READINGS = (
    Reading("wind_speed_mps", "m/s"),
    Reading("wind_direction_deg", "deg"),
    Reading("rotor_speed_rpm", "rpm"),
    Reading("generator_speed_rpm", "rpm"),
    Reading("power_kw", "kW"),
    Reading("pitch_angle_deg", "deg"),
    Reading("ambient_temp_c", "degC"),
    Reading("gearbox_oil_temp_c", "degC"),
    Reading("gearbox_bearing_temp_c", "degC"),
    Reading("main_bearing_temp_c", "degC"),
    Reading("generator_bearing_temp_c", "degC"),
    Reading("nacelle_vibration_mm_s", "mm/s"),
)
UNITS = {reading.name: reading.unit for reading in READINGS}
MAXIMUM_TEXT = 64


class MalformedError(ValueError):
    """A sample breaks the schema. Its reason is one short, fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Parsed:
    tenant_id: str
    turbine_id: str
    sequence: int
    event_time: datetime
    source: str
    schema_version: str
    model_version: str
    synthetic: bool
    operating_state: str
    available: bool
    readings: dict[str, float | None]


def _text(sample: dict[str, Any], name: str) -> str:
    value = sample.get(name)
    if not isinstance(value, str) or not value or len(value) > MAXIMUM_TEXT:
        raise MalformedError(f"bad_{name}")
    return value


def parse(sample: object) -> Parsed:
    """Check one sample against its schema, or raise MalformedError with the reason."""
    if not isinstance(sample, dict):
        raise MalformedError("not_an_object")
    version = sample.get("schema_version")
    if version not in SCHEMA_VERSIONS:
        raise MalformedError("unknown_schema_version")
    sequence = sample.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise MalformedError("bad_sequence")
    raw_time = sample.get("event_time")
    if not isinstance(raw_time, str):
        raise MalformedError("bad_event_time")
    try:
        event_time = datetime.fromisoformat(raw_time)
    except ValueError as error:
        raise MalformedError("bad_event_time") from error
    if event_time.tzinfo is None:
        raise MalformedError("bad_event_time")
    readings: dict[str, float | None] = {}
    for reading in READINGS:
        value = sample.get(reading.name)
        if value is None:
            readings[reading.name] = None
        elif isinstance(value, bool) or not isinstance(value, int | float):
            raise MalformedError(f"bad_{reading.name}")
        else:
            readings[reading.name] = float(value)
    for flag in ("synthetic", "available"):
        if not isinstance(sample.get(flag), bool):
            raise MalformedError(f"bad_{flag}")
    return Parsed(
        tenant_id=_text(sample, "tenant_id"),
        turbine_id=_text(sample, "turbine_id"),
        sequence=sequence,
        event_time=event_time,
        source=_text(sample, "source"),
        schema_version=str(version),
        model_version=_text(sample, "model_version"),
        synthetic=bool(sample["synthetic"]),
        operating_state=_text(sample, "operating_state"),
        available=bool(sample["available"]),
        readings=readings,
    )


# --- CMS vibration batches (FR-TEL-004)

CMS_SCHEMA_VERSIONS = frozenset({"1"})
CMS_SENSORS = frozenset({"main_bearing", "gearbox", "generator"})
CMS_UNITS = frozenset({"g"})
# A window is a power of two long, from 1024 to 16384 values, so one batch stays well under 1 MiB.
CMS_WINDOW_LENGTHS = frozenset(2**n for n in range(10, 15))
CMS_SAMPLE_RATES = range(1000, 51201)


@dataclass(frozen=True)
class CmsWindow:
    sensor: str
    unit: str
    values: list[float]


@dataclass(frozen=True)
class CmsBatch:
    tenant_id: str
    turbine_id: str
    sequence: int
    event_time: datetime
    source: str
    schema_version: str
    model_version: str
    synthetic: bool
    rotor_speed_rpm: float
    generator_speed_rpm: float
    sample_rate_hz: int
    windows: list[CmsWindow]
    trigger: str | None


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def parse_cms(batch: object) -> CmsBatch:
    """Check one CMS batch against its schema, or raise MalformedError with the reason."""
    if not isinstance(batch, dict):
        raise MalformedError("not_an_object")
    version = batch.get("schema_version")
    if version not in CMS_SCHEMA_VERSIONS:
        raise MalformedError("unknown_schema_version")
    sequence = batch.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise MalformedError("bad_sequence")
    raw_time = batch.get("event_time")
    try:
        event_time = datetime.fromisoformat(raw_time) if isinstance(raw_time, str) else None
    except ValueError:
        event_time = None
    if event_time is None or event_time.tzinfo is None:
        raise MalformedError("bad_event_time")
    for name in ("rotor_speed_rpm", "generator_speed_rpm"):
        if not _number(batch.get(name)) or batch[name] < 0:
            raise MalformedError(f"bad_{name}")
    rate = batch.get("sample_rate_hz")
    if not isinstance(rate, int) or isinstance(rate, bool) or rate not in CMS_SAMPLE_RATES:
        raise MalformedError("bad_sample_rate_hz")
    if not isinstance(batch.get("synthetic"), bool):
        raise MalformedError("bad_synthetic")
    sensors = batch.get("sensors")
    if not isinstance(sensors, list) or not 1 <= len(sensors) <= len(CMS_SENSORS):
        raise MalformedError("bad_sensors")
    windows, seen = [], set()
    for item in sensors:
        if not isinstance(item, dict) or item.get("sensor") not in CMS_SENSORS:
            raise MalformedError("unknown_sensor")
        if item["sensor"] in seen:
            raise MalformedError("repeated_sensor")
        seen.add(item["sensor"])
        if item.get("unit") not in CMS_UNITS:
            raise MalformedError("bad_unit")
        values = item.get("values")
        if not isinstance(values, list) or len(values) not in CMS_WINDOW_LENGTHS:
            raise MalformedError("bad_window_length")
        if not all(_number(v) for v in values):
            raise MalformedError("bad_values")
        windows.append(CmsWindow(item["sensor"], item["unit"], [float(v) for v in values]))
    trigger = batch.get("trigger")
    if trigger is not None and (
        not isinstance(trigger, str) or not 0 < len(trigger) <= MAXIMUM_TEXT
    ):
        raise MalformedError("bad_trigger")
    return CmsBatch(
        tenant_id=_text(batch, "tenant_id"),
        turbine_id=_text(batch, "turbine_id"),
        sequence=sequence,
        event_time=event_time,
        source=_text(batch, "source"),
        schema_version=str(version),
        model_version=_text(batch, "model_version"),
        synthetic=bool(batch["synthetic"]),
        rotor_speed_rpm=float(batch["rotor_speed_rpm"]),
        generator_speed_rpm=float(batch["generator_speed_rpm"]),
        sample_rate_hz=rate,
        windows=windows,
        trigger=trigger,
    )


# What a turbine's controller sends when it raises or clears an alarm (FR-ALM-001). The Telemetry
# Service checks only its shape and its device; the Alarm Service owns what it means.
ALARM_SCHEMA_VERSIONS = frozenset({"1"})
ALARM_ACTIONS = frozenset({"raise", "clear"})
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
MAXIMUM_MESSAGE = 200
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_REPORT_ID = re.compile(r"[A-Za-z0-9:._-]{1,128}")


@dataclass(frozen=True)
class ParsedAlarm:
    tenant_id: str
    turbine_id: str
    report_id: str
    code: str
    tag: str
    action: str
    event_time: datetime
    severity: str
    message: str
    source: str


def _optional(report: dict[str, Any], name: str, limit: int) -> str:
    value = report.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise MalformedError(f"bad_{name}")
    return value


def parse_alarm(report: object) -> ParsedAlarm:
    """Check one alarm report against its schema, or raise MalformedError with the reason."""
    if not isinstance(report, dict):
        raise MalformedError("not_an_object")
    if report.get("schema_version") not in ALARM_SCHEMA_VERSIONS:
        raise MalformedError("unknown_schema_version")
    code, report_id = report.get("code"), report.get("report_id")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise MalformedError("bad_code")
    if not isinstance(report_id, str) or not _REPORT_ID.fullmatch(report_id):
        raise MalformedError("bad_report_id")
    action = report.get("action")
    if action not in ALARM_ACTIONS:
        raise MalformedError("bad_action")
    raw_time = report.get("event_time")
    try:
        event_time = datetime.fromisoformat(raw_time) if isinstance(raw_time, str) else None
    except ValueError:
        event_time = None
    if event_time is None or event_time.tzinfo is None:
        raise MalformedError("bad_event_time")
    severity = _optional(report, "severity", MAXIMUM_TEXT)
    if severity and severity not in SEVERITIES:
        raise MalformedError("bad_severity")
    return ParsedAlarm(
        tenant_id=_text(report, "tenant_id"),
        turbine_id=_text(report, "turbine_id"),
        report_id=report_id,
        code=code,
        tag=_optional(report, "tag", MAXIMUM_TEXT),
        action=str(action),
        event_time=event_time,
        severity=severity,
        message=_optional(report, "message", MAXIMUM_MESSAGE),
        source=_text(report, "source"),
    )
