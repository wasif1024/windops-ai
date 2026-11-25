"""The tables this service owns, all tenant-owned with row-level security, and its audit events.

Nothing here holds evidence, reasons, names or addresses (FR-NOT-002): identifiers, the text a
notification shows, and whether it was delivered.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "notification"
IN_APP, EMAIL = "in_app", "email"
CHANNELS = (IN_APP, EMAIL)
# A delivery's status: in-app ones are delivered as they are made; email waits for its sender.
DELIVERED, PENDING, RECORDED, FAILED = "delivered", "pending", "recorded", "failed"
DELIVERY_STATUSES = (DELIVERED, PENDING, RECORDED, FAILED)
# An event heard, while whom to tell is worked out: "waiting", "done", or "skipped" with why.
WAITING, DONE, SKIPPED = "waiting", "done", "skipped"
METADATA = MetaData()

# Each event that may tell someone something, kept once by its ID (EVT-003). What it says is only
# identifiers, as JSON. Whom to tell is worked out after, so an outage of Authorization or Asset
# only delays it (SRS 7.7).
HEARD = Table(
    "heard_events",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("event_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("facts", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", BigInteger, nullable=False, server_default="0"),
    Column("note", Text, nullable=False, server_default=""),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("heard_events_waiting", "status", "next_attempt_at"),
)
# Who drafted and submitted each work order, from workorder.submitted, so its decision reaches
# them. Kept at its newest version.
WORK_ORDERS = Table(
    "work_orders",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("work_order_id", Text, primary_key=True),
    Column("version", BigInteger, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("turbine_id", Text, nullable=False),
    Column("drafted_by", Text, nullable=False),
    Column("submitted_by", Text, nullable=False),
)
# One a person and event: what the app shows them.
NOTIFICATIONS = Table(
    "notifications",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("notification_id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("link", Text, nullable=False),
    Column("case_id", Text, nullable=False, server_default=""),
    Column("work_order_id", Text, nullable=False, server_default=""),
    Column("event_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("read_at", DateTime(timezone=True)),
    UniqueConstraint("tenant_id", "event_id", "user_id", name="one_notification_a_person"),
    Index("notifications_by_person", "tenant_id", "user_id", "created_at"),
)
# Each channel a notification goes by, and how that went (FR-NOT-001).
DELIVERIES = Table(
    "deliveries",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("notification_id", Text, primary_key=True),
    Column("channel", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("provider", Text, nullable=False, server_default=""),
    Column("attempts", BigInteger, nullable=False, server_default="0"),
    Column("last_error", Text, nullable=False, server_default=""),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("deliveries_due", "status", "next_attempt_at"),
)
# A tenant's own rules, replacing the defaults while it has any.
RULES = Table(
    "notification_rules",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("audience", Text, primary_key=True),
    # "in_app", "email" or "in_app,email".
    Column("channels", Text, nullable=False),
    Column("set_by", Text, nullable=False),
    Column("set_at", DateTime(timezone=True), nullable=False),
)
# Set while a tenant has its own rules, even none for a kind.
CUSTOMIZED = Table(
    "customized_tenants",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("set_by", Text, nullable=False),
    Column("set_at", DateTime(timezone=True), nullable=False),
)
# Each change to the rules, by its idempotency key, so a retry changes nothing more (GRP-007).
RULE_CHANGES = Table(
    "rule_changes",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("set_by", Text, nullable=False),
    Column("set_at", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = (
    "heard_events", "work_orders", "notifications", "deliveries", "notification_rules",
    "customized_tenants", "rule_changes",
)  # fmt: skip


async def audit(
    connection: AsyncConnection, *, tenant_id: str, actor_id: str, action: str,
    resource_type: str, resource_id: str, outcome: str = "succeeded",
) -> None:  # fmt: skip
    """Ask the Audit Service to record it, in the same transaction (EVT-002, EVT-004)."""
    await add_event(
        connection, audit_request_subject(SERVICE_NAME), tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action, actor_type="user", actor_id=actor_id, resource_type=resource_type,
            resource_id=resource_id, outcome=outcome,
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip


def utc(value: datetime) -> datetime:
    """SQLite gives back naive times; PostgreSQL, aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
