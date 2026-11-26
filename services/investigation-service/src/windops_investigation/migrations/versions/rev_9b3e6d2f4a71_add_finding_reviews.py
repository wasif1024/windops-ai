"""Add each person's review of a finding in an investigation's report (FR-INV-006, M11.1).

Revision ID: 9b3e6d2f4a71
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "9b3e6d2f4a71"
down_revision: str | None = "2a7d4f9c1e85"
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "case_finding_reviews",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("review_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("finding", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('accept', 'reject', 'challenge', 'annotate')",
            name="case_finding_reviews_action",
        ),
    )
    op.create_index(
        "case_finding_reviews_by_case", "case_finding_reviews", ["tenant_id", "case_id"]
    )
    # A review is kept as made: never changed, never deleted.
    op.execute(
        "CREATE FUNCTION case_finding_reviews_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A finding review never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER case_finding_reviews_never_change BEFORE UPDATE OR DELETE ON "
        "case_finding_reviews FOR EACH ROW EXECUTE FUNCTION case_finding_reviews_never_change()"
    )
    enable_tenant_isolation(op.get_bind(), "case_finding_reviews")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
