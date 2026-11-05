"""Turn on row-level security for grants, role permissions and break-glass (DAT-005).

Revision ID: f5a7b9c1d324
"""

from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "f5a7b9c1d324"
down_revision: str | None = "c4e81f3a6b20"
# Code that does not name its tenant sees nothing once this runs, so it runs only after every
# running version names one, which the release before this did.
phase: str = "contract"


def upgrade() -> None:
    # idempotency_keys has no tenant: it is keyed by user, and replays only after a fresh check.
    for table in ("role_grants", "role_permissions", "customized_tenants", "break_glass_grants"):
        enable_tenant_isolation(op.get_bind(), table)
