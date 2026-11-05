"""Every tenant-owned table turns on row-level security in its migrations (DAT-005).

`make cross-tenant-check` asks PostgreSQL the same question on the cluster.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted(ROOT.glob("services/*/src/*/migrations/versions/*.py"))
# Tables with a tenant_id that do not need it, and why.
EXEMPT = {
    # Only each service's relay reads it, never a request, and it reads every tenant's rows.
    "outbox_events",
}


def tables_with_a_tenant(source: str) -> set[str]:
    found = set()
    for match in re.finditer(r'op\.create_table\(\s*"(\w+)",(.*?)\n    \)', source, re.S):
        if re.search(r'sa\.Column\(\s*"tenant_id"', match.group(2)):
            found.add(match.group(1))
    return found


def isolated_tables(source: str) -> set[str]:
    direct = set(re.findall(r'enable_tenant_isolation\(op\.get_bind\(\), "(\w+)"\)', source))
    looped: set[str] = set()
    for group in re.findall(r"for table in \(([^)]*)\):\s*\n\s*enable_tenant_isolation", source):
        looped |= set(re.findall(r'"(\w+)"', group)) | set(re.findall(r"'(\w+)'", group))
    return direct | looped


def by_service() -> dict[str, tuple[set[str], set[str]]]:
    services: dict[str, tuple[set[str], set[str]]] = {}
    for path in MIGRATIONS:
        service = path.parts[len(ROOT.parts) + 1]
        created, isolated = services.setdefault(service, (set(), set()))
        source = path.read_text()
        created |= tables_with_a_tenant(source)
        isolated |= isolated_tables(source)
    return services


def test_the_checks_find_the_tables() -> None:
    services = by_service()
    assert "audit_records" in services["audit-service"][0]
    assert {"tenants", "branding"} <= services["tenant-service"][1]


@pytest.mark.parametrize("service", sorted(by_service()))
def test_every_tenant_table_has_row_level_security(service: str) -> None:
    created, isolated = by_service()[service]
    assert created - EXEMPT - isolated == set()


def test_the_rules_catch_a_table_left_out() -> None:
    source = """
    op.create_table(
        "readings",
        sa.Column("tenant_id", sa.Text(), nullable=False),
    )
"""
    assert tables_with_a_tenant(source) == {"readings"}
    assert isolated_tables(source) == set()
