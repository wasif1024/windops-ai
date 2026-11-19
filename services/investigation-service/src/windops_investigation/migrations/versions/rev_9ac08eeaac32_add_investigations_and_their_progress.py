"""Add each case's investigation, and its progress, which never changes.

Revision ID: 9ac08eeaac32
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "9ac08eeaac32"
down_revision: str | None = "436ab17d1529"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "case_investigations",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("delegation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("delegation_expires_at", sa.DateTime(timezone=True)),
        sa.Column("run_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("snapshot_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "case_progress",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("position", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_steps", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("step", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("event_key", sa.Text(), nullable=False),
    )
    op.create_index(
        "case_progress_once", "case_progress", ["tenant_id", "case_id", "event_key"], unique=True
    )
    # An event ID, once sent, always means the same event (API-003).
    op.execute(
        "CREATE FUNCTION case_progress_never_changes() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A progress event never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER case_progress_never_changes BEFORE UPDATE OR DELETE ON case_progress "
        "FOR EACH ROW EXECUTE FUNCTION case_progress_never_changes()"
    )
    enable_tenant_isolation(op.get_bind(), "case_investigations")
    enable_tenant_isolation(op.get_bind(), "case_progress")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
