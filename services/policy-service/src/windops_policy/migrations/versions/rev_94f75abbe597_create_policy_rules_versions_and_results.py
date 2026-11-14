"""Create policy rules, their versions, results and idempotency keys, by tenant, and the outbox.

Revision ID: 94f75abbe597
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "94f75abbe597"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "policy_rules",
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
        "policy_rule_versions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("rule_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('limit', 'match')", name="policy_rule_versions_kind"),
    )
    op.create_table(
        "policy_results",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("result_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("evaluated_by", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('pass', 'block', 'escalate')", name="policy_results_outcome"
        ),
    )
    op.create_index(
        "policy_results_by_case", "policy_results", ["tenant_id", "case_id", "evaluated_at"]
    )
    # A version, or a result, once written never changes: not by this service, nor by anyone with
    # its login (FR-POL-001, FR-POL-003).
    op.execute(
        "CREATE FUNCTION policy_records_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A policy rule version or result never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER policy_rule_versions_never_change BEFORE UPDATE OR DELETE ON "
        "policy_rule_versions FOR EACH ROW EXECUTE FUNCTION policy_records_never_change()"
    )
    op.execute(
        "CREATE TRIGGER policy_results_never_change BEFORE UPDATE OR DELETE ON "
        "policy_results FOR EACH ROW EXECUTE FUNCTION policy_records_never_change()"
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
    enable_tenant_isolation(op.get_bind(), "policy_rules")
    enable_tenant_isolation(op.get_bind(), "policy_rule_versions")
    enable_tenant_isolation(op.get_bind(), "policy_results")
    enable_tenant_isolation(op.get_bind(), "idempotency_keys")
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
