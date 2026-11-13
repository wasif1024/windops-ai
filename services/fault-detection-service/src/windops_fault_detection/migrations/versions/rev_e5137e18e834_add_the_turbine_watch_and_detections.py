"""Add the turbine watch and detections, isolated by tenant.

Revision ID: e5137e18e834
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "e5137e18e834"
down_revision: str | None = "55f3680ceba9"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"

SEVERITIES = "'low', 'medium', 'high', 'critical'"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "turbine_watch",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "detections",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("detection_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.BigInteger(), nullable=False),
        sa.Column("rule_content_hash", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("observed", sa.Text(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True)),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("telemetry_snapshot_id", sa.Text(), nullable=False),
        sa.Column("telemetry_snapshot_hash", sa.Text(), nullable=False),
        sa.Column("alarm_ids", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_reason", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint("state IN ('open', 'closed')", name="detections_state"),
        sa.CheckConstraint(f"severity IN ({SEVERITIES})", name="detections_severity"),
    )
    # At most one open detection a rule on a turbine.
    op.create_index(
        "detections_one_open", "detections", ["tenant_id", "turbine_id", "rule_id"],
        unique=True, postgresql_where=sa.text("state = 'open'"),
    )  # fmt: skip
    op.create_index(
        "detections_by_turbine", "detections", ["tenant_id", "turbine_id", "detected_at"]
    )
    for table in ("turbine_watch", "detections"):
        enable_tenant_isolation(op.get_bind(), table)


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
