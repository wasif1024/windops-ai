"""What an asset record may hold. The service checks every field before it stores one."""

import re
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
COUNTRY = re.compile(r"^[A-Z]{2}$")
MAXIMUM_TEXT = 200

TURBINE_STATUSES = frozenset(
    {"operating", "curtailed", "stopped", "maintenance", "fault", "offline"}
)
COMPONENT_STATUSES = frozenset({"in_service", "degraded", "failed", "removed"})
# The parts of a turbine the SRS names, and those its fault profiles need (SRS 10.4).
COMPONENT_KINDS = frozenset(
    {
        "tower",
        "nacelle",
        "rotor",
        "hub",
        "blade",
        "pitch_system",
        "main_shaft",
        "main_bearing",
        "gearbox",
        "gearbox_bearing",
        "gearbox_oil_system",
        "generator",
        "generator_bearing",
        "converter",
        "transformer",
        "yaw_system",
        "cooling_system",
        "controller",
        "sensor",
    }
)


class AssetRuleError(ValueError):
    """A field breaks a rule, whoever sends it."""


def check_id(name: str, value: str) -> None:
    if not ID.match(value):
        raise AssetRuleError(f"{name} is lowercase letters, digits and hyphens, 2 to 63 long")


def check_text(name: str, value: str, *, required: bool = False) -> None:
    if required and not value.strip():
        raise AssetRuleError(f"{name} is required")
    if len(value) > MAXIMUM_TEXT:
        raise AssetRuleError(f"{name} is at most {MAXIMUM_TEXT} characters")


def check_location(latitude: float, longitude: float) -> None:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise AssetRuleError(
            "A location is a latitude from -90 to 90 and a longitude from -180 to 180"
        )


def check_country(value: str) -> None:
    if not COUNTRY.match(value):
        raise AssetRuleError("country is an ISO 3166-1 code, such as GB")


def check_time_zone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise AssetRuleError("time_zone is an IANA time zone, such as Europe/London") from error


def check_date(name: str, value: str) -> None:
    if not value:
        return
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise AssetRuleError(f"{name} is a date such as 2024-05-17") from error


def check_model(rated_power_kw: float, rotor_diameter_m: float, hub_height_m: float) -> None:
    for name, value in (
        ("rated_power_kw", rated_power_kw),
        ("rotor_diameter_m", rotor_diameter_m),
        ("hub_height_m", hub_height_m),
    ):
        if value < 0:
            raise AssetRuleError(f"{name} cannot be negative")


def check_status(kind: str, status: str, reason: str) -> None:
    """A turbine's or component's status, with a reason for anything but the normal one."""
    allowed, normal = (
        (TURBINE_STATUSES, "operating") if kind == "turbine" else (COMPONENT_STATUSES, "in_service")
    )
    if status not in allowed:
        raise AssetRuleError(f"A {kind}'s status is one of: {', '.join(sorted(allowed))}")
    if status != normal and not reason.strip():
        raise AssetRuleError(f"Give a reason for a status other than {normal}")
    check_text("reason", reason)


def check_component_kind(kind: str) -> None:
    if kind not in COMPONENT_KINDS:
        raise AssetRuleError(f"kind is one of: {', '.join(sorted(COMPONENT_KINDS))}")
