"""Add each case's staleness, and the proposals the stale rules watch (SRS 8.5, M10.3).

Revision ID: d83804ad5767
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "d83804ad5767"
down_revision: str | None = "9ac08eeaac32"
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.add_column("cases", sa.Column("stale_rule", sa.Text(), nullable=False, server_default=""))
    op.add_column("cases", sa.Column("stale_detail", sa.Text(), nullable=False, server_default=""))
    op.add_column("cases", sa.Column("stale_at", sa.DateTime(timezone=True)))
    op.add_column(
        "cases", sa.Column("stale_rules_version", sa.Text(), nullable=False, server_default="")
    )
    op.create_table(
        "case_proposals",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot_id", sa.Text(), nullable=False),
        sa.Column("parts", sa.Text(), nullable=False),
        sa.Column("procedures", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("case_proposals_by_case", "case_proposals", ["tenant_id", "case_id"])
    enable_tenant_isolation(op.get_bind(), "case_proposals")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
