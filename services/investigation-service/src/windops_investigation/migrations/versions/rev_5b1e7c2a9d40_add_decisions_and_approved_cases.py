"""Add each case's decisions, and let a case be approved (SRS 5.4 step 14, M10.4).

Revision ID: 5b1e7c2a9d40
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "5b1e7c2a9d40"
down_revision: str | None = "d83804ad5767"
# "expand": a wider status constraint and a wider index beside the old ones, which a contract
# revision then drops. The version before M10.4 never writes "approved".
phase: str = "expand"

ACTIVE = "('open', 'investigating', 'pending_review', 'approved')"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_check_constraint(
        "cases_status_with_approved",
        "cases",
        "status IN ('open', 'investigating', 'pending_review', 'approved', 'closed', 'cancelled')",
    )
    op.create_index(
        "cases_one_active_or_approved", "cases", ["tenant_id", "turbine_id", "component_group"],
        unique=True, postgresql_where=sa.text(f"origin = 'detection' AND status IN {ACTIVE}"),
    )  # fmt: skip
    op.create_table(
        "case_decisions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("approval_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", sa.Text(), nullable=False),
        sa.Column("proposal_version", sa.BigInteger(), nullable=False),
        sa.Column("evidence_snapshot_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("resumes_run_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("run_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("references_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="case_decisions_decision"),
        sa.CheckConstraint(
            "status IN ('waiting', 'running', 'completed', 'ended', 'recorded')",
            name="case_decisions_status",
        ),
    )
    op.create_index("case_decisions_by_case", "case_decisions", ["tenant_id", "case_id"])
    enable_tenant_isolation(op.get_bind(), "case_decisions")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
