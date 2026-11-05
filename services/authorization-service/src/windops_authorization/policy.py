"""The roles, the SRS 3.2 matrix, and how a decision is made. Pure, so each rule is testable."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

# A tenant of its own, for platform staff, so every token still carries a tenant (SEC-005).
PLATFORM_TENANT = "windops-platform"

PLATFORM_ADMINISTRATOR = "platform-administrator"
TENANT_ADMINISTRATOR = "tenant-administrator"
OPERATIONS_MANAGER = "operations-manager"
CONTROL_ROOM_OPERATOR = "control-room-operator"
RELIABILITY_ENGINEER = "reliability-engineer"
MAINTENANCE_PLANNER = "maintenance-planner"
MAINTENANCE_MANAGER = "maintenance-manager"
AUDITOR = "auditor"

ROLES = (
    PLATFORM_ADMINISTRATOR,
    TENANT_ADMINISTRATOR,
    OPERATIONS_MANAGER,
    CONTROL_ROOM_OPERATOR,
    RELIABILITY_ENGINEER,
    MAINTENANCE_PLANNER,
    MAINTENANCE_MANAGER,
    AUDITOR,
)
# The abbreviations SRS 3.2 uses, so a test can compare this module with the document.
ABBREVIATIONS = {
    "PA": PLATFORM_ADMINISTRATOR,
    "TA": TENANT_ADMINISTRATOR,
    "OM": OPERATIONS_MANAGER,
    "CO": CONTROL_ROOM_OPERATOR,
    "RE": RELIABILITY_ENGINEER,
    "MP": MAINTENANCE_PLANNER,
    "MM": MAINTENANCE_MANAGER,
    "AU": AUDITOR,
}


def _matrix(rows: Mapping[str, str]) -> dict[str, frozenset[str]]:
    by_role: dict[str, set[str]] = {role: set() for role in ROLES}
    for permission, holders in rows.items():
        for short in holders.split(", "):
            by_role[ABBREVIATIONS[short]].add(permission)
    return {role: frozenset(permissions) for role, permissions in by_role.items()}


# SRS 3.2, row for row.
_DEFAULT_ROWS = {
    "turbine.read": "TA, OM, CO, RE, MP, MM, AU",
    "asset.manage": "TA",
    "telemetry.read": "OM, CO, RE, MP, MM, AU",
    "alarm.read": "OM, CO, RE, MP, MM, AU",
    "alarm.acknowledge": "CO, OM",
    "investigation.create": "OM, CO, RE",
    "investigation.read": "OM, CO, RE, MP, MM, AU",
    "investigation.cancel": "OM, RE",
    "finding.review": "OM, RE",
    "maintenance.draft": "MP",
    "maintenance.submit": "MP",
    "maintenance.approve": "MM",
    "maintenance.complete": "MP, MM",
    "inventory.read": "OM, RE, MP, MM, AU",
    "inventory.update": "MP",
    "inventory.reserve": "MP",
    "weather.read": "OM, CO, RE, MP, MM",
    "knowledge.read": "OM, CO, RE, MP, MM, AU",
    "knowledge.manage": "TA, RE",
    "policy.read": "TA, OM, RE, MM, AU",
    "policy.manage": "TA",
    "users.manage": "PA, TA",
    "roles.manage": "PA, TA",
    "permissions.manage": "PA, TA",
    "tenant.manage": "PA",
    "branding.manage": "TA",
    "audit.read": "PA, TA, AU",
    "gateway.configure": "PA",
    "mcp.configure": "PA, TA",
    "evaluation.manage": "PA",
}
PERMISSIONS = frozenset(_DEFAULT_ROWS)
DEFAULT_MATRIX = _matrix(_DEFAULT_ROWS)
# Platform-wide, so no tenant can give them to one of its own roles.
PLATFORM_ONLY = frozenset({"tenant.manage", "gateway.configure", "evaluation.manage"})
TENANT_PERMISSIONS = PERMISSIONS - PLATFORM_ONLY
# A Tenant Administrator cannot remove these from their own role, or nobody could manage roles.
TENANT_ADMINISTRATOR_KEEPS = frozenset({"roles.manage", "permissions.manage"})
# What a Platform Administrator may do in any tenant without break-glass: administration, not data.
CROSS_TENANT_ADMINISTRATION = frozenset(
    {"users.manage", "roles.manage", "permissions.manage", "tenant.manage", "mcp.configure"}
)
# What break-glass allows: reading a tenant's data, and never changing it (RBAC-003).
BREAK_GLASS_PERMISSIONS = frozenset(p for p in PERMISSIONS if p.endswith(".read"))
REASON_MINIMUM = 20
DURATION_DEFAULT_MINUTES = 60
DURATION_MAXIMUM_MINUTES = 240


@dataclass(frozen=True)
class Scope:
    wind_farm_id: str = ""
    turbine_id: str = ""

    @property
    def whole_tenant(self) -> bool:
        return not self.wind_farm_id and not self.turbine_id


@dataclass(frozen=True)
class Grant:
    role: str
    scope: Scope


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    break_glass: bool = False


def covers(granted: Scope, requested: Scope) -> bool:
    """Whether a grant's scope includes the requested one (RBAC-002)."""
    if granted.whole_tenant:
        return True
    if granted.turbine_id:
        return granted.turbine_id == requested.turbine_id
    return granted.wind_farm_id == requested.wind_farm_id


def permissions_of(role: str, custom: Mapping[str, frozenset[str]] | None) -> frozenset[str]:
    """A role's permissions in one tenant: its own mapping once customized, or the defaults."""
    if custom is not None and role != PLATFORM_ADMINISTRATOR:
        return custom.get(role, frozenset())
    return DEFAULT_MATRIX[role]


def decide_in_tenant(
    permission: str,
    scope: Scope,
    grants: Iterable[Grant],
    custom: Mapping[str, frozenset[str]] | None,
) -> Decision:
    """Decide for a user acting in their own tenant. Deny unless a grant allows it."""
    if permission not in PERMISSIONS:
        return Decision(False, "Unknown permission")
    for grant in grants:
        if permission in permissions_of(grant.role, custom) and covers(grant.scope, scope):
            return Decision(True, f"Granted by the {grant.role} role")
    return Decision(False, "No role grants this permission here")


def decide_across_tenants(
    permission: str,
    *,
    platform_grants: Iterable[Grant],
    break_glass_until: datetime | None,
    now: datetime,
) -> Decision:
    """Decide for platform staff acting in another tenant (RBAC-003)."""
    if permission not in PERMISSIONS:
        return Decision(False, "Unknown permission")
    if not any(grant.role == PLATFORM_ADMINISTRATOR for grant in platform_grants):
        return Decision(False, "Only a Platform Administrator can act in another tenant")
    if permission in CROSS_TENANT_ADMINISTRATION:
        return Decision(True, "Platform administration")
    if (
        permission in BREAK_GLASS_PERMISSIONS
        and break_glass_until is not None
        and now < break_glass_until
    ):
        return Decision(True, "Break-glass access", break_glass=True)
    return Decision(False, "A tenant's data needs an active break-glass grant")


class RuleError(ValueError):
    """A request breaks a rule about roles or permissions, whoever makes it."""


def check_grantable(tenant_id: str, role: str, scope: Scope) -> None:
    if role not in ROLES:
        raise RuleError("Unknown role")
    if (role == PLATFORM_ADMINISTRATOR) != (tenant_id == PLATFORM_TENANT):
        raise RuleError("The Platform Administrator role exists only in the platform tenant")
    if role == PLATFORM_ADMINISTRATOR and not scope.whole_tenant:
        raise RuleError("The Platform Administrator role cannot be scoped")


def check_mapping(role: str, permissions: Iterable[str]) -> frozenset[str]:
    """Return the validated set of a role's permissions, for one tenant."""
    chosen = frozenset(permissions)
    if role not in ROLES:
        raise RuleError("Unknown role")
    if role == PLATFORM_ADMINISTRATOR:
        raise RuleError("A tenant cannot change the Platform Administrator role")
    if chosen - PERMISSIONS:
        raise RuleError("Unknown permission")
    if chosen & PLATFORM_ONLY:
        raise RuleError("Platform permissions cannot be given to a tenant's role")
    if role == TENANT_ADMINISTRATOR and not chosen >= TENANT_ADMINISTRATOR_KEEPS:
        raise RuleError(
            "The Tenant Administrator role must keep roles.manage and permissions.manage"
        )
    return chosen


def check_break_glass(tenant_id: str, reason: str, duration_minutes: int) -> int:
    """Return the duration to use, in minutes."""
    if tenant_id == PLATFORM_TENANT:
        raise RuleError("Break-glass is for a customer tenant")
    if len(reason.strip()) < REASON_MINIMUM:
        raise RuleError(f"A reason of at least {REASON_MINIMUM} characters is required")
    duration = duration_minutes or DURATION_DEFAULT_MINUTES
    if not 1 <= duration <= DURATION_MAXIMUM_MINUTES:
        raise RuleError(f"Break-glass lasts from 1 to {DURATION_MAXIMUM_MINUTES} minutes")
    return duration
