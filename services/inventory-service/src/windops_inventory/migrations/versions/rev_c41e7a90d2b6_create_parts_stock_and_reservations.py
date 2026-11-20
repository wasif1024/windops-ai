"""Create the parts catalog, stock, reservations, the stock ledger and idempotency keys, by
tenant, and the outbox.

Revision ID: c41e7a90d2b6
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "c41e7a90d2b6"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "parts",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("part_number", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("component_kind", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("fits_any_model", sa.Boolean(), nullable=False),
        sa.Column("retired", sa.Boolean(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
    )
    op.create_table(
        "part_models",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("part_number", sa.Text(), primary_key=True),
        sa.Column("manufacturer_key", sa.Text(), primary_key=True),
        sa.Column("model_key", sa.Text(), primary_key=True),
        sa.Column("manufacturer", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
    )
    op.create_index(
        "part_models_by_model", "part_models", ["tenant_id", "manufacturer_key", "model_key"]
    )
    op.create_table(
        "stock",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("part_number", sa.Text(), primary_key=True),
        sa.Column("wind_farm_id", sa.Text(), primary_key=True),
        sa.Column("on_hand", sa.BigInteger(), nullable=False),
        sa.Column("reserved", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # What is held can never be more than what is there.
        sa.CheckConstraint("reserved >= 0 AND on_hand >= reserved", name="stock_covers_reserved"),
    )
    op.create_table(
        "reservations",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("reservation_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), nullable=False),
        sa.Column("part_number", sa.Text(), nullable=False),
        sa.Column("wind_farm_id", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reserved_by", sa.Text(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("release_reason", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("reservations_by_work_order", "reservations", ["tenant_id", "work_order_id"])
    op.create_table(
        "stock_movements",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("part_number", sa.Text(), primary_key=True),
        sa.Column("wind_farm_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("on_hand", sa.BigInteger(), nullable=False),
        sa.Column("reserved", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("reservation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
    )
    # The stock ledger is kept as it happened: never changed, never deleted.
    op.execute(
        "CREATE FUNCTION stock_movements_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'The stock ledger never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER stock_movements_never_change BEFORE UPDATE OR DELETE ON "
        "stock_movements FOR EACH ROW EXECUTE FUNCTION stock_movements_never_change()"
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
    for table in (
        "parts",
        "part_models",
        "stock",
        "reservations",
        "stock_movements",
        "idempotency_keys",
    ):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
