"""What a device sends, at each schema version, and each reading's unit (FR-TEL-001, FR-TEL-002).

A reading's unit is fixed by its name and the schema version, so it is stored once here, not on
every row. The Telemetry Service accepts the simulator's schema version 2 (windops-simulator).
"""

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
