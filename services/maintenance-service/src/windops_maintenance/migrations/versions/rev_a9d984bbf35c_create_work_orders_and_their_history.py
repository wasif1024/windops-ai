"""Create work orders, their history and idempotency keys, by tenant, and the outbox.

Revision ID: a9d984bbf35c
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "a9d984bbf35c"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "work_orders",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot_id", sa.Text(), nullable=False),
        sa.Column("plan", sa.LargeBinary(), nullable=False),
        sa.Column("validation", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("drafted_by", sa.Text(), nullable=False),
        sa.Column("drafted_by_agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("work_orders_by_case", "work_orders", ["tenant_id", "case_id"])
    op.create_table(
        "work_order_history",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )
    # A work order's history is kept as it happened: never changed, never deleted.
    op.execute(
        "CREATE FUNCTION work_order_history_never_changes() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A work order''s history never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER work_order_history_never_changes BEFORE UPDATE OR DELETE ON "
        "work_order_history FOR EACH ROW EXECUTE FUNCTION work_order_history_never_changes()"
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("caller", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # DAT-005. The outbox is not here: only the relay reads it, never a request.
    for table in ("work_orders", "work_order_history", "idempotency_keys"):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
