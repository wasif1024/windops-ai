"""Create role grants, tenant role mappings, break-glass grants, idempotency keys and the outbox.

Revision ID: c4e81f3a6b20
"""

import sqlalchemy as sa
from alembic import op
from windops_outbox import create_outbox_table

revision: str = "c4e81f3a6b20"
down_revision: str | None = None
phase: str = "expand"


def upgrade() -> None:
    op.create_table(
        "role_grants",
        sa.Column("grant_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        # Empty for the whole tenant, so equality works without NULL rules (RBAC-002).
        sa.Column("wind_farm_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("turbine_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("granted_by", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.Text()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("role_grants_by_user", "role_grants", ["tenant_id", "user_id"])
    op.create_table(
        "customized_tenants",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("customized_by", sa.Text(), nullable=False),
        sa.Column("customized_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "role_permissions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("role", sa.Text(), primary_key=True),
        sa.Column("permission", sa.Text(), primary_key=True),
    )
    op.create_table(
        "break_glass_grants",
        sa.Column("grant_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_by", sa.Text()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index("break_glass_by_user", "break_glass_grants", ["tenant_id", "user_id"])
    # A retried call returns its first answer (GRP-007). Keys belong to the user who sent them.
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_outbox_table(op.get_bind())
