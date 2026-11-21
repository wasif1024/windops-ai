"""Add why a work order is stale, and its earlier versions, kept as they were (M10.3).

Revision ID: 41d06bcd24a7
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "41d06bcd24a7"
down_revision: str | None = "a9d984bbf35c"
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.add_column(
        "work_orders", sa.Column("stale_rule", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        "work_orders", sa.Column("stale_detail", sa.Text(), nullable=False, server_default="")
    )
    op.create_table(
        "work_order_versions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("work_order", sa.LargeBinary(), nullable=False),
        sa.Column("revised_at", sa.DateTime(timezone=True), nullable=False),
    )
    # An earlier version is kept as it was: never changed, never deleted (EVD-008).
    op.execute(
        "CREATE FUNCTION work_order_versions_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'An earlier work order version never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER work_order_versions_never_change BEFORE UPDATE OR DELETE ON "
        "work_order_versions FOR EACH ROW EXECUTE FUNCTION work_order_versions_never_change()"
    )
    enable_tenant_isolation(op.get_bind(), "work_order_versions")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
