"""Create runs, the queue itself, and their completed steps, by tenant, and the outbox.

A completed step never changes: a run taken over by another replica carries on after it.

Revision ID: d7b7f4172717
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "d7b7f4172717"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "runs",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("workflow_version", sa.Text(), nullable=False),
        sa.Column("parameters", sa.Text(), nullable=False),
        sa.Column("total_steps", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("max_attempts", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("cancel_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("lease_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("lease_owner", sa.Text(), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', "
            "'budget_exhausted', 'stale')",
            name="runs_status",
        ),
        sa.CheckConstraint("attempt >= 0 AND attempt <= max_attempts", name="runs_attempts"),
        # Only a running run has a lease.
        sa.CheckConstraint(
            "(status = 'running') = (lease_token <> '' AND lease_expires_at IS NOT NULL)",
            name="runs_lease",
        ),
    )
    op.create_index("runs_by_case", "runs", ["tenant_id", "case_id", "created_at"])
    op.create_index("runs_one_key", "runs", ["tenant_id", "idempotency_key"], unique=True)
    op.create_index("runs_by_status", "runs", ["status", "not_before"])
    op.create_table(
        "run_steps",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("worker", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.run_id"]),
    )
    op.execute(
        "CREATE FUNCTION run_steps_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A completed step never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER run_steps_never_change BEFORE UPDATE OR DELETE ON run_steps "
        "FOR EACH ROW EXECUTE FUNCTION run_steps_never_change()"
    )
    # DAT-005. The outbox is not here: only the relay reads it, never a request.
    enable_tenant_isolation(op.get_bind(), "runs")
    enable_tenant_isolation(op.get_bind(), "run_steps")
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
