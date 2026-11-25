"""Create the events heard, work orders' people, notifications, deliveries and rules, by tenant,
and the outbox (M10.5).

Revision ID: 7d2c9e4a1b63
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "7d2c9e4a1b63"
down_revision: str | None = None
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"

TIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "heard_events",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("facts", sa.Text(), nullable=False),
        sa.Column("occurred_at", TIME, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_attempt_at", TIME, nullable=False),
        sa.Column("received_at", TIME, nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
        sa.CheckConstraint("status IN ('waiting', 'done', 'skipped')", name="heard_status"),
    )
    op.create_index("heard_events_waiting", "heard_events", ["status", "next_attempt_at"])
    op.create_table(
        "work_orders",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("drafted_by", sa.Text(), nullable=False),
        sa.Column("submitted_by", sa.Text(), nullable=False),
    )
    op.create_table(
        "notifications",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("notification_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("work_order_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("read_at", TIME),
        sa.UniqueConstraint("tenant_id", "event_id", "user_id", name="one_notification_a_person"),
    )
    op.create_index("notifications_by_person", "notifications",
                    ["tenant_id", "user_id", "created_at"])  # fmt: skip
    op.create_table(
        "deliveries",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("notification_id", sa.Text(), primary_key=True),
        sa.Column("channel", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_attempt_at", TIME, nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
        sa.CheckConstraint("channel IN ('in_app', 'email')", name="deliveries_channel"),
        sa.CheckConstraint(
            "status IN ('delivered', 'pending', 'recorded', 'failed')", name="deliveries_status"
        ),
    )
    op.create_index("deliveries_due", "deliveries", ["status", "next_attempt_at"])
    op.create_table(
        "notification_rules",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("audience", sa.Text(), primary_key=True),
        sa.Column("channels", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", TIME, nullable=False),
    )
    op.create_table(
        "customized_tenants",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", TIME, nullable=False),
    )
    # DAT-005. The outbox is not here: only the relay reads it, never a request.
    op.create_table(
        "rule_changes",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", TIME, nullable=False),
    )
    enable_tenant_isolation(op.get_bind(), "heard_events")
    enable_tenant_isolation(op.get_bind(), "work_orders")
    enable_tenant_isolation(op.get_bind(), "notifications")
    enable_tenant_isolation(op.get_bind(), "deliveries")
    enable_tenant_isolation(op.get_bind(), "notification_rules")
    enable_tenant_isolation(op.get_bind(), "customized_tenants")
    enable_tenant_isolation(op.get_bind(), "rule_changes")
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
