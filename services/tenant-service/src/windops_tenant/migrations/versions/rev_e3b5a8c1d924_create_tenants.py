"""Create tenants, branding, email templates and custom domains, isolated by tenant, and the outbox.

Revision ID: e3b5a8c1d924
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "e3b5a8c1d924"
down_revision: str | None = None
phase: str = "expand"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("suspended_reason", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="tenants_status"),
    )
    op.create_table(
        "branding",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("primary_color", sa.Text(), nullable=False),
        sa.Column("accent_color", sa.Text(), nullable=False),
        sa.Column("login_title", sa.Text(), nullable=False),
        sa.Column("login_message", sa.Text(), nullable=False),
        sa.Column("time_zone", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("logo", sa.LargeBinary()),
        sa.Column("logo_type", sa.Text()),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "email_templates",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("template", sa.Text(), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "custom_domains",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("domain", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("verification_token", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # DAT-005. The outbox is not here: only the relay reads it, never a request.
    for table in ("tenants", "branding", "email_templates", "custom_domains"):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())
