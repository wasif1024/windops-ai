"""Create the audit records, their append-only rules, and the outbox.

Revision ID: a1f0c7d2e9b4
"""

import sqlalchemy as sa
from alembic import op
from windops_outbox import create_outbox_table

revision: str = "a1f0c7d2e9b4"
down_revision: str | None = None
phase: str = "expand"


def upgrade() -> None:
    op.create_table(
        "audit_records",
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Text(), nullable=False, unique=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("on_behalf_of", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("previous_hash", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.Text(), nullable=False),
        # A record has one successor, so even a bug cannot fork a tenant's chain.
        sa.UniqueConstraint("tenant_id", "previous_hash", name="audit_records_one_successor"),
    )
    op.create_index("audit_records_tenant", "audit_records", ["tenant_id", "sequence"])
    # FR-AUD-002: refused for every login, including this service's own.
    op.execute(
        """
        CREATE FUNCTION audit_records_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit records are append-only';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_records_no_change BEFORE UPDATE OR DELETE ON audit_records
        FOR EACH ROW EXECUTE FUNCTION audit_records_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_records_no_truncate BEFORE TRUNCATE ON audit_records
        FOR EACH STATEMENT EXECUTE FUNCTION audit_records_append_only()
        """
    )
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
