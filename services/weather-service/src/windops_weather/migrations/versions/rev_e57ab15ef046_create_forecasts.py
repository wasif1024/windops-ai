"""Create the forecasts table, isolated by tenant, whose rows never change.

Revision ID: e57ab15ef046
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "e57ab15ef046"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "forecasts",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("forecast_id", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("elevation_m", sa.Float(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hours", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
    )
    op.create_index(
        "forecasts_newest", "forecasts", ["tenant_id", "latitude", "longitude", "retrieved_at"]
    )
    # A retrieval, once kept, never changes: a policy result may point to it (WTH-003, M6.4).
    op.execute(
        "CREATE FUNCTION forecasts_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A kept forecast never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER forecasts_never_change BEFORE UPDATE OR DELETE ON forecasts "
        "FOR EACH ROW EXECUTE FUNCTION forecasts_never_change()"
    )
    # DAT-005.
    enable_tenant_isolation(op.get_bind(), "forecasts")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
