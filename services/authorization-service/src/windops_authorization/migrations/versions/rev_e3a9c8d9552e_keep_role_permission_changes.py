"""Keep every change to role permissions, by tenant (RBAC-005, RBAC-006).

Revision ID: e3a9c8d9552e
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "e3a9c8d9552e"
down_revision: str | None = "f5a7b9c1d324"
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "role_permission_changes",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("previous", sa.Text(), nullable=False),
        sa.Column("permissions", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Kept as it happened: never changed, never deleted.
    op.execute(
        "CREATE FUNCTION role_permission_changes_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A role permission change is never changed'; END $$"
    )
    op.execute(
        "CREATE TRIGGER role_permission_changes_never_change BEFORE UPDATE OR DELETE ON "
        "role_permission_changes FOR EACH ROW EXECUTE FUNCTION "
        "role_permission_changes_never_change()"
    )
    enable_tenant_isolation(op.get_bind(), "role_permission_changes")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
