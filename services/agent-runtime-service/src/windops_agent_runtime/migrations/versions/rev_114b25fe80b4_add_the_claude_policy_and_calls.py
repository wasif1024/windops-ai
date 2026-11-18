"""Pin each run's Claude policy and budget, and record each call to Claude, by tenant.

A recorded call never changes: what was spent is what was spent (LLM-010).

Revision ID: 114b25fe80b4
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "114b25fe80b4"
down_revision: str | None = "d7b7f4172717"
# "expand" keeps the database working for the running version (DAT-001). The new columns have
# defaults, so the version before this one keeps writing runs.
phase: str = "expand"


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("policy_version", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column("runs", sa.Column("policy_hash", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "runs", sa.Column("token_budget", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "runs", sa.Column("cost_budget_usd", sa.Numeric(12, 6), nullable=False, server_default="0")
    )
    op.create_table(
        "run_calls",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), primary_key=True),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("worker", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("requested_model", sa.Text(), nullable=False),
        sa.Column("served_model", sa.Text(), nullable=False),
        sa.Column("effort", sa.Text(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=False),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_5m_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_1h_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("processed_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.run_id"]),
    )
    op.execute(
        "CREATE FUNCTION run_calls_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A recorded call never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER run_calls_never_change BEFORE UPDATE OR DELETE ON run_calls "
        "FOR EACH ROW EXECUTE FUNCTION run_calls_never_change()"
    )
    enable_tenant_isolation(op.get_bind(), "run_calls")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
