"""Add LangGraph's checkpoints, each run's pins, and pausing (M8.4).

Checkpoints and their writes never change, as a resumed run carries on from what they hold.

Revision ID: 34322b04af91
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "34322b04af91"
down_revision: str | None = "114b25fe80b4"
# "expand" keeps the database working for the running version (DAT-001). The new columns have
# defaults, and the version before this one never writes "paused".
phase: str = "expand"


def upgrade() -> None:
    op.add_column("runs", sa.Column("pins", sa.Text(), nullable=False, server_default="{}"))
    op.add_column("runs", sa.Column("pause_requested_at", sa.DateTime(timezone=True)))
    op.add_column(
        "runs", sa.Column("pause_requested_by", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("runs", sa.Column("pause_reason", sa.Text(), nullable=False, server_default=""))
    # Wider than runs_status, which a contract revision then drops: it forbids "paused".
    op.create_check_constraint(
        "runs_status_with_paused",
        "runs",
        "status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled', "
        "'budget_exhausted', 'stale')",
    )
    op.create_table(
        "run_checkpoints",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("namespace", sa.Text(), primary_key=True),
        sa.Column("checkpoint_id", sa.Text(), primary_key=True),
        sa.Column("parent_id", sa.Text(), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("worker", sa.Text(), nullable=False),
        sa.Column("checkpoint_type", sa.Text(), nullable=False),
        sa.Column("checkpoint", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_type", sa.Text(), nullable=False),
        sa.Column("metadata", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.run_id"]),
    )
    op.create_table(
        "run_checkpoint_writes",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("namespace", sa.Text(), primary_key=True),
        sa.Column("checkpoint_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("idx", sa.BigInteger(), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.Column("task_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.run_id"]),
    )
    op.execute(
        "CREATE FUNCTION checkpoints_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A checkpoint never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER run_checkpoints_never_change BEFORE UPDATE OR DELETE ON run_checkpoints "
        "FOR EACH ROW EXECUTE FUNCTION checkpoints_never_change()"
    )
    op.execute(
        "CREATE TRIGGER run_checkpoint_writes_never_change BEFORE UPDATE OR DELETE "
        "ON run_checkpoint_writes FOR EACH ROW EXECUTE FUNCTION checkpoints_never_change()"
    )
    enable_tenant_isolation(op.get_bind(), "run_checkpoints")
    enable_tenant_isolation(op.get_bind(), "run_checkpoint_writes")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
