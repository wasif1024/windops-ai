"""The alarm catalogue: what each controller code means, and its default severity (FR-ALM-004).

A device usually sends its own severity, and that is kept. When it sends none, the catalogue's
default is used, and the alarm records the catalogue's version, so anyone can see where its
severity came from. A code the catalogue does not know gets MEDIUM, from the same version.

Severity comes only from these two places. No other code path sets it, and nothing changes it
after an alarm is made: not an operator, and never a model.
"""

from dataclasses import dataclass

VERSION = "alarm-catalog-1"
SEVERITIES = ("low", "medium", "high", "critical")
UNKNOWN_SEVERITY = "medium"
DEVICE = "device"


@dataclass(frozen=True)
class Entry:
    code: str
    description: str
    severity: str
    component: str


ENTRIES = (
    Entry("GBX_OIL_TEMP_HIGH", "Gearbox oil temperature high", "high", "gearbox"),
    Entry("GBX_OIL_TEMP_CRITICAL", "Gearbox oil temperature critical", "critical", "gearbox"),
    Entry("GBX_BEARING_TEMP_HIGH", "Gearbox bearing temperature high", "high", "gearbox_bearing"),
    Entry("MAIN_BEARING_TEMP_HIGH", "Main bearing temperature high", "high", "main_bearing"),
    Entry("GEN_BEARING_TEMP_HIGH", "Generator bearing temperature high", "high",
          "generator_bearing"),
    Entry("NACELLE_VIBRATION_HIGH", "Nacelle vibration high", "medium", "nacelle"),
    Entry("SENSOR_SIGNAL_LOST", "A sensor sends no signal", "low", "controller"),
)  # fmt: skip
CATALOG = {entry.code: entry for entry in ENTRIES}


def severity_for(code: str, from_device: str) -> tuple[str, str]:
    """The severity to keep, and where it came from: the device, or this catalogue."""
    if from_device in SEVERITIES:
        return from_device, DEVICE
    entry = CATALOG.get(code)
    return (entry.severity if entry else UNKNOWN_SEVERITY), f"catalog:{VERSION}"


def description_for(code: str, from_device: str) -> str:
    entry = CATALOG.get(code)
    return entry.description if entry else (from_device or code)
