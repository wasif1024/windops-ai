"""Run each service's Alembic migrations as a Kubernetes Job (DAT-001, DAT-002)."""

from windops_migrations.check import MigrationViolation, find_violations
from windops_migrations.runner import (
    MissingSettingError,
    create_revision,
    database_url,
    migrations_directory,
    run_migrations,
    upgrade,
)
from windops_migrations.tenancy import across_tenants, enable_tenant_isolation, in_tenant

__all__ = [
    "MigrationViolation",
    "MissingSettingError",
    "across_tenants",
    "create_revision",
    "database_url",
    "enable_tenant_isolation",
    "find_violations",
    "in_tenant",
    "migrations_directory",
    "run_migrations",
    "upgrade",
]
