"""The tables this service owns. All are tenant-owned, so they have row-level security."""

from sqlalchemy import Column, DateTime, LargeBinary, MetaData, Table, Text
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "tenant"
METADATA = MetaData()
TENANTS = Table(
    "tenants",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("suspended_reason", Text),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
BRANDING = Table(
    "branding",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("primary_color", Text, nullable=False),
    Column("accent_color", Text, nullable=False),
    Column("login_title", Text, nullable=False),
    Column("login_message", Text, nullable=False),
    Column("time_zone", Text, nullable=False),
    Column("locale", Text, nullable=False),
    Column("logo", LargeBinary),
    Column("logo_type", Text),
    Column("updated_by", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
EMAIL_TEMPLATES = Table(
    "email_templates",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("template", Text, primary_key=True),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("updated_by", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
# One domain per tenant, and one tenant per domain.
CUSTOM_DOMAINS = Table(
    "custom_domains",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("domain", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Column("verification_token", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = ("tenants", "branding", "email_templates", "custom_domains")


async def audit(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    action: str,
    actor_id: str,
    resource_id: str,
    actor_type: str = "user",
) -> None:
    """Queue an audit record in the same transaction as the change (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type="tenant",
            resource_id=resource_id,
            outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )
