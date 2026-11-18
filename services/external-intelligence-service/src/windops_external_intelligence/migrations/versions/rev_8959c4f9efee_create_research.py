"""Create research, extractions, research policies and idempotency keys, by tenant, and the outbox.

A search and an extraction, once kept, never change: a finding may cite them (EXT-003).

Revision ID: 8959c4f9efee
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "8959c4f9efee"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "research",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("research_id", sa.Text(), primary_key=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("data_gap", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("outbound_query", sa.Text(), nullable=False),
        sa.Column("redactions", sa.Text(), nullable=False),
        sa.Column("wind_farm_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("turbine_class", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_request_id", sa.Text(), nullable=False),
        sa.Column("response_seconds", sa.Float(), nullable=False),
        sa.Column("provider_called", sa.Boolean(), nullable=False),
        sa.Column("sources", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('complete', 'unavailable')", name="research_state"),
    )
    op.create_index("research_by_time", "research", ["tenant_id", "requested_at"])
    op.create_table(
        "extractions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("research_id", sa.Text(), primary_key=True),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("flags", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("extracted_by", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "research_id"], ["research.tenant_id", "research.research_id"]
        ),
    )
    op.create_index("extractions_by_time", "extractions", ["tenant_id", "retrieved_at"])
    op.create_table(
        "research_policies",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allowed_domains", sa.Text(), nullable=False),
        sa.Column("blocked_domains", sa.Text(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
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
    op.execute(
        "CREATE FUNCTION research_never_changes() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'Kept research never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER research_never_changes BEFORE UPDATE OR DELETE ON research "
        "FOR EACH ROW EXECUTE FUNCTION research_never_changes()"
    )
    op.execute(
        "CREATE TRIGGER extractions_never_change BEFORE UPDATE OR DELETE ON extractions "
        "FOR EACH ROW EXECUTE FUNCTION research_never_changes()"
    )
    # DAT-005. The outbox is not here: only the relay reads it, never a request.
    enable_tenant_isolation(op.get_bind(), "research")
    enable_tenant_isolation(op.get_bind(), "extractions")
    enable_tenant_isolation(op.get_bind(), "research_policies")
    enable_tenant_isolation(op.get_bind(), "idempotency_keys")
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
