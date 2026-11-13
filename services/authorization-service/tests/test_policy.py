"""The decision rules, which need no server or database."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from windops_authorization import policy
from windops_authorization.policy import Grant, RuleError, Scope

SRS = Path(__file__).resolve().parents[3] / "docs" / "srs.md"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
FARM, OTHER_FARM, TURBINE = "farm-1", "farm-2", "turbine-7"


def srs_matrix() -> dict[str, frozenset[str]]:
    """Read SRS 3.2's table, so the code and the document cannot drift apart."""
    section = SRS.read_text().split("### 3.2 Default role-permission matrix")[1].split("### 3.3")[0]
    rows = re.findall(r"^\| ([a-z]+\.[a-z]+) \| (?:Yes|No) \| ([A-Z, ]+) \|$", section, re.M)
    assert len(rows) == 30
    by_role: dict[str, set[str]] = {role: set() for role in policy.ROLES}
    for permission, holders in rows:
        for short in holders.split(", "):
            by_role[policy.ABBREVIATIONS[short]].add(permission)
    return {role: frozenset(permissions) for role, permissions in by_role.items()}


def test_the_matrix_matches_srs_3_2_exactly() -> None:
    assert srs_matrix() == policy.DEFAULT_MATRIX


def test_there_are_the_eight_srs_roles() -> None:
    assert len(policy.ROLES) == 8
    assert set(policy.ABBREVIATIONS.values()) == set(policy.ROLES)


def grants(role: str, scope: Scope | None = None) -> list[Grant]:
    return [Grant(role, scope or Scope())]


def test_nothing_is_allowed_without_a_grant() -> None:
    decision = policy.decide_in_tenant("turbine.read", Scope(), [], None)
    assert not decision.allowed


def test_a_role_allows_only_its_own_permissions() -> None:
    operator = grants(policy.CONTROL_ROOM_OPERATOR)
    assert policy.decide_in_tenant("alarm.acknowledge", Scope(), operator, None).allowed
    assert not policy.decide_in_tenant("maintenance.approve", Scope(), operator, None).allowed


def test_an_unknown_permission_is_denied_even_for_an_administrator() -> None:
    decision = policy.decide_in_tenant(
        "everything.do", Scope(), grants(policy.TENANT_ADMINISTRATOR), None
    )
    assert not decision.allowed


@pytest.mark.parametrize(
    ("granted", "requested", "allowed"),
    [
        (Scope(), Scope(), True),
        (Scope(), Scope(FARM, TURBINE), True),
        (Scope(FARM), Scope(FARM), True),
        (Scope(FARM), Scope(FARM, TURBINE), True),
        (Scope(FARM), Scope(OTHER_FARM, TURBINE), False),
        (Scope(FARM), Scope(), False),
        (Scope("", TURBINE), Scope(FARM, TURBINE), True),
        (Scope("", TURBINE), Scope(FARM, "turbine-8"), False),
        (Scope("", TURBINE), Scope(FARM), False),
    ],
)
def test_a_scoped_grant_covers_only_its_scope(
    granted: Scope, requested: Scope, allowed: bool
) -> None:
    """RBAC-002. A tenant-wide request needs a tenant-wide grant."""
    operator = grants(policy.CONTROL_ROOM_OPERATOR, granted)
    assert policy.decide_in_tenant("alarm.read", requested, operator, None).allowed is allowed


def test_a_customized_tenant_uses_its_own_mapping() -> None:
    custom = {policy.CONTROL_ROOM_OPERATOR: frozenset({"turbine.read"})}
    operator = grants(policy.CONTROL_ROOM_OPERATOR)
    assert policy.decide_in_tenant("turbine.read", Scope(), operator, custom).allowed
    assert not policy.decide_in_tenant("alarm.acknowledge", Scope(), operator, custom).allowed


def platform_decision(
    permission: str, *, role: str = policy.PLATFORM_ADMINISTRATOR, until: datetime | None = None
) -> policy.Decision:
    return policy.decide_across_tenants(
        permission, platform_grants=grants(role), break_glass_until=until, now=NOW
    )


def test_a_platform_administrator_administers_any_tenant() -> None:
    assert platform_decision("users.manage").allowed
    assert platform_decision("tenant.manage").allowed


def test_a_platform_administrator_cannot_read_tenant_data_without_break_glass() -> None:
    """RBAC-003, and SRS 3.1: no routine access to a tenant's operational data."""
    for permission in ("turbine.read", "telemetry.read", "audit.read", "investigation.read"):
        assert not platform_decision(permission).allowed


def test_break_glass_allows_reading_and_says_so() -> None:
    decision = platform_decision("telemetry.read", until=NOW + timedelta(minutes=5))
    assert decision.allowed
    assert decision.break_glass


def test_break_glass_never_allows_a_change() -> None:
    until = NOW + timedelta(minutes=5)
    for permission in ("maintenance.approve", "alarm.acknowledge", "asset.manage"):
        assert not platform_decision(permission, until=until).allowed


def test_expired_break_glass_allows_nothing() -> None:
    assert not platform_decision("telemetry.read", until=NOW).allowed


def test_only_a_platform_administrator_can_act_in_another_tenant() -> None:
    until = NOW + timedelta(minutes=5)
    decision = platform_decision("users.manage", role=policy.TENANT_ADMINISTRATOR, until=until)
    assert not decision.allowed


@pytest.mark.parametrize(
    ("tenant", "role", "scope"),
    [
        ("north-sea", "owner", Scope()),
        ("north-sea", policy.PLATFORM_ADMINISTRATOR, Scope()),
        (policy.PLATFORM_TENANT, policy.TENANT_ADMINISTRATOR, Scope()),
        (policy.PLATFORM_TENANT, policy.PLATFORM_ADMINISTRATOR, Scope(FARM)),
    ],
)
def test_roles_that_cannot_be_granted(tenant: str, role: str, scope: Scope) -> None:
    with pytest.raises(RuleError):
        policy.check_grantable(tenant, role, scope)


def test_a_scoped_role_can_be_granted() -> None:
    policy.check_grantable("north-sea", policy.CONTROL_ROOM_OPERATOR, Scope(FARM))


@pytest.mark.parametrize(
    ("role", "permissions"),
    [
        (policy.PLATFORM_ADMINISTRATOR, ["users.manage"]),
        (policy.CONTROL_ROOM_OPERATOR, ["tenant.manage"]),
        (policy.CONTROL_ROOM_OPERATOR, ["gateway.configure"]),
        (policy.CONTROL_ROOM_OPERATOR, ["not.real"]),
        (policy.TENANT_ADMINISTRATOR, ["users.manage", "roles.manage"]),
        ("owner", ["turbine.read"]),
    ],
)
def test_mappings_that_are_refused(role: str, permissions: list[str]) -> None:
    with pytest.raises(RuleError):
        policy.check_mapping(role, permissions)


def test_a_role_can_lose_every_permission() -> None:
    assert policy.check_mapping(policy.AUDITOR, []) == frozenset()


@pytest.mark.parametrize(
    ("tenant", "reason", "minutes"),
    [
        (policy.PLATFORM_TENANT, "A long enough reason for access", 60),
        ("north-sea", "too short", 60),
        ("north-sea", "      too short, padded       ", 60),
        ("north-sea", "A long enough reason for access", 241),
    ],
)
def test_break_glass_requests_that_are_refused(tenant: str, reason: str, minutes: int) -> None:
    with pytest.raises(RuleError):
        policy.check_break_glass(tenant, reason, minutes)


def test_break_glass_lasts_an_hour_unless_asked() -> None:
    assert policy.check_break_glass("north-sea", "Investigating ticket INC-4411 now", 0) == 60


def test_a_service_can_never_be_granted_a_change_or_an_approval() -> None:
    """SEC-009: whatever is added to the service grants, they stay read-only."""
    for service, permissions in policy.SERVICE_GRANTS.items():
        assert all(p.endswith(".read") for p in permissions), service
        assert policy.service_of(f"service:{service}") == service
    assert policy.service_of("a-user-subject") is None
