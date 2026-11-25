"""Add each completed work order's verification, and let a case be verifying or verified (M10.6).

Revision ID: 6c4f1a8e2b57
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "6c4f1a8e2b57"
down_revision: str | None = "8f3a6d1c4e27"
# "expand": a wider status constraint and a wider index beside the old ones, which a contract
# revision then drops. The version before M10.6 never writes "verifying" or "verified".
phase: str = "expand"

ACTIVE = "('open', 'investigating', 'pending_review', 'approved', 'verifying', 'verified')"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_check_constraint(
        "cases_status_with_verification",
        "cases",
        "status IN ('open', 'investigating', 'pending_review', 'approved', 'verifying', "
        "'verified', 'closed', 'cancelled')",
    )
    op.create_index(
        "cases_one_active", "cases", ["tenant_id", "turbine_id", "component_group"],
        unique=True, postgresql_where=sa.text(f"origin = 'detection' AND status IN {ACTIVE}"),
    )  # fmt: skip
    op.create_table(
        "case_verifications",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), primary_key=True),
        sa.Column("proposal_version", sa.BigInteger(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot_id", sa.Text(), nullable=False),
        sa.Column("completed_by", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False, server_default=""),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("before_telemetry_snapshot_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("after_telemetry_snapshot_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("after_samples", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("metrics_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("rules_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True)),
        sa.Column("review", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('waiting', 'reported', 'not_started')", name="case_verifications_status"
        ),
        sa.CheckConstraint(
            "outcome IN ('', 'resolved', 'not_resolved', 'inconclusive')",
            name="case_verifications_outcome",
        ),
        sa.CheckConstraint(
            "review IN ('', 'closed', 'reopened')", name="case_verifications_review"
        ),
    )
    op.create_index("case_verifications_by_case", "case_verifications", ["tenant_id", "case_id"])
    enable_tenant_isolation(op.get_bind(), "case_verifications")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
