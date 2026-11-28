"""Add each tenant's egress switches, isolated by tenant (EGR-002, ADR 0048).

A path with no row is on. A row is written the first time someone turns it off or on.

Revision ID: e716fbfbe140
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "e716fbfbe140"
down_revision: str | None = "e3b5a8c1d924"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    op.create_table(
        "egress_paths",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "path IN ('claude', 'embeddings', 'langsmith_traces', 'langsmith_sandbox', "
            "'forecasts')",
            name="egress_paths_path",
        ),
    )
    enable_tenant_isolation(op.get_bind(), "egress_paths")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
