"""Create wind farms, turbines, components and idempotency keys, isolated by tenant, and the outbox.

Revision ID: a3e343f95165
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "a3e343f95165"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"

TURBINE_STATUSES = "'operating', 'curtailed', 'stopped', 'maintenance', 'fault', 'offline'"
COMPONENT_STATUSES = "'in_service', 'degraded', 'failed', 'removed'"


def upgrade() -> None:
    op.create_table(
        "wind_farms",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("wind_farm_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("time_zone", sa.Text(), nullable=False),
        sa.Column("offshore", sa.Boolean(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "turbines",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), primary_key=True),
        sa.Column("wind_farm_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("manufacturer", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("rated_power_kw", sa.Float(), nullable=False),
        sa.Column("rotor_diameter_m", sa.Float(), nullable=False),
        sa.Column("hub_height_m", sa.Float(), nullable=False),
        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("commissioned_on", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # A turbine belongs to a wind farm of its own tenant.
        sa.ForeignKeyConstraint(
            ["tenant_id", "wind_farm_id"], ["wind_farms.tenant_id", "wind_farms.wind_farm_id"]
        ),
        sa.CheckConstraint(f"status IN ({TURBINE_STATUSES})", name="turbines_status"),
    )
    op.create_index("turbines_by_wind_farm", "turbines", ["tenant_id", "wind_farm_id"])
    op.create_table(
        "components",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("component_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("parent_component_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("manufacturer", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("installed_on", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "turbine_id"], ["turbines.tenant_id", "turbines.turbine_id"]
        ),
        sa.CheckConstraint(f"status IN ({COMPONENT_STATUSES})", name="components_status"),
    )
    op.create_index("components_by_turbine", "components", ["tenant_id", "turbine_id"])
    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # DAT-005. The outbox is not here: only the relay reads it, never a request.
    for table in ("wind_farms", "turbines", "components", "idempotency_keys"):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
