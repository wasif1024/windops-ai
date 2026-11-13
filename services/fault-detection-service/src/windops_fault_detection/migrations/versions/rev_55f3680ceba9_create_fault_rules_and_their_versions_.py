"""Create fault rules, their versions and idempotency keys, isolated by tenant, and the outbox.

Revision ID: 55f3680ceba9
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "55f3680ceba9"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"

KINDS = "'threshold', 'residual', 'rolling_zscore', 'alarm_correlation', 'data_gap'"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "fault_rules",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("rule_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "fault_rule_versions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("rule_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="fault_rule_versions_kind"),
    )
    # A version, once written, never changes: not by this service, nor by anyone with its login.
    op.execute(
        "CREATE FUNCTION fault_rule_versions_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A fault rule version never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER fault_rule_versions_never_change BEFORE UPDATE OR DELETE ON "
        "fault_rule_versions FOR EACH ROW EXECUTE FUNCTION fault_rule_versions_never_change()"
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
    for table in ("fault_rules", "fault_rule_versions", "idempotency_keys"):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
