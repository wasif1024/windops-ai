"""Turn on row-level security for created_users (DAT-005).

Revision ID: a8c0e2f4b635
"""

from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "a8c0e2f4b635"
down_revision: str | None = "d7a2c9e4f611"
# Code that does not name its tenant sees nothing once this runs, so it runs only after every
# running version names one, which the release before this did.
phase: str = "contract"


def upgrade() -> None:
    for table in ("created_users",):
        enable_tenant_isolation(op.get_bind(), table)
