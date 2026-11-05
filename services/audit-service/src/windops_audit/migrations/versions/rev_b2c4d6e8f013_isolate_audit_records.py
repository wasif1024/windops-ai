"""Turn on row-level security for audit_records (DAT-005).

Revision ID: b2c4d6e8f013
"""

from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "b2c4d6e8f013"
down_revision: str | None = "a1f0c7d2e9b4"
# Code that does not name its tenant sees nothing once this runs, so it runs only after every
# running version names one, which the release before this did.
phase: str = "contract"


def upgrade() -> None:
    for table in ("audit_records",):
        enable_tenant_isolation(op.get_bind(), table)
