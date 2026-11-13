"""Create cases, their detections and history, and idempotency keys, by tenant, and the outbox.

Revision ID: f6d84e59d898
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "f6d84e59d898"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"

STATUSES = "'open', 'investigating', 'pending_review', 'closed', 'cancelled'"
SEVERITIES = "'low', 'medium', 'high', 'critical'"
ACTIVE = "status IN ('open', 'investigating', 'pending_review')"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "cases",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("component_group", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detection_at", sa.DateTime(timezone=True)),
        sa.Column("case_rules_version", sa.Text(), nullable=False),
        sa.Column("cancel_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="cases_status"),
        sa.CheckConstraint(f"severity IN ({SEVERITIES})", name="cases_severity"),
        sa.CheckConstraint("origin IN ('detection', 'user')", name="cases_origin"),
    )
    # At most one active case from detections for a component of a turbine (FR-FLT-004).
    op.create_index(
        "cases_one_active", "cases", ["tenant_id", "turbine_id", "component_group"],
        unique=True, postgresql_where=sa.text(f"origin = 'detection' AND {ACTIVE}"),
    )  # fmt: skip
    op.create_index("cases_by_turbine", "cases", ["tenant_id", "turbine_id", "updated_at"])
    op.create_table(
        "case_detections",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("detection_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("rule_content_hash", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("telemetry_snapshot_id", sa.Text(), nullable=False),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("case_detections_by_case", "case_detections", ["tenant_id", "case_id"])
    op.create_table(
        "case_history",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
    )
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
    for table in ("cases", "case_detections", "case_history", "idempotency_keys"):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
