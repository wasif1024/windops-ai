"""Add changes to who may approve maintenance, waiting for a second person (RBAC-007).

Revision ID: e59b0bf5c77d
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "e59b0bf5c77d"
down_revision: str | None = "e3a9c8d9552e"
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "pending_changes",
        sa.Column("change_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("request", sa.LargeBinary(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("grantee_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("grant_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("permissions", sa.Text(), nullable=False, server_default=""),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("escalated", sa.Boolean(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decline_reason", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'declined')", name="pending_change_status"
        ),
    )
    op.create_index("pending_changes_by_tenant", "pending_changes", ["tenant_id", "status"])
    enable_tenant_isolation(op.get_bind(), "pending_changes")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
