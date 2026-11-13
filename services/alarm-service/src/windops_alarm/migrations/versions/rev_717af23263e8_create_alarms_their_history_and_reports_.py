"""Create alarms, their history, reports and idempotency keys, isolated by tenant, and the outbox.

Revision ID: 717af23263e8
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "717af23263e8"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"

STATES = "'active', 'acknowledged', 'cleared', 'shelved'"
SEVERITIES = "'low', 'medium', 'high', 'critical'"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "alarms",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("alarm_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("severity_source", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column("shelved_until", sa.DateTime(timezone=True)),
        sa.Column("shelved_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("shelve_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("shelved_from", sa.Text(), nullable=False, server_default=""),
        sa.Column("repeat_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"state IN ({STATES})", name="alarms_state"),
        sa.CheckConstraint(f"severity IN ({SEVERITIES})", name="alarms_severity"),
    )
    # At most one standing alarm for a condition on a turbine.
    op.create_index(
        "alarms_one_standing", "alarms", ["tenant_id", "turbine_id", "code", "tag"],
        unique=True, postgresql_where=sa.text("state <> 'cleared'"),
    )  # fmt: skip
    op.create_index("alarms_by_turbine", "alarms", ["tenant_id", "turbine_id", "raised_at"])
    op.create_table(
        "alarm_transitions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("alarm_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
    )
    op.create_table(
        "alarm_reports",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), primary_key=True),
        sa.Column("report_id", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("alarm_id", sa.Text(), nullable=False, server_default=""),
    )
    # Finds a clear waiting for its raise.
    op.create_index(
        "alarm_reports_waiting", "alarm_reports",
        ["tenant_id", "turbine_id", "code", "tag", "event_time"],
        postgresql_where=sa.text("NOT applied"),
    )  # fmt: skip
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
    for table in ("alarms", "alarm_transitions", "alarm_reports", "idempotency_keys"):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
