"""Create delegations, which let a run's agents act for one principal, by tenant (M8.5).

Revision ID: fe0e718d7570
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "fe0e718d7570"
down_revision: str | None = "a8c0e2f4b635"
# "expand": a new table, which the running version never reads.
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "delegations",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("delegation_id", sa.Text(), primary_key=True),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("end_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "principal = 'service:fault-investigator' OR principal LIKE 'user:%'",
            name="delegations_principal",
        ),
        sa.CheckConstraint("expires_at > created_at", name="delegations_expiry"),
    )
    op.create_index(
        "delegations_one_key",
        "delegations",
        ["tenant_id", "started_by", "idempotency_key"],
        unique=True,
    )
    enable_tenant_isolation(op.get_bind(), "delegations")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
