"""Create impact references, their versions, results and idempotency keys, and the outbox.

Revision ID: 229347701414
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "229347701414"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "impact_references",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("reference_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("current_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "impact_reference_versions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("reference_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('power_curve', 'energy_price', 'maintenance_costs')",
            name="impact_reference_versions_kind",
        ),
    )
    op.create_table(
        "impact_results",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("result_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("estimated_by", sa.Text(), nullable=False),
        sa.Column("estimated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "impact_results_by_case", "impact_results", ["tenant_id", "case_id", "estimated_at"]
    )
    # A version, or a result, once written never changes: not by this service, nor by anyone with
    # its login (FR-IMP-003).
    op.execute(
        "CREATE FUNCTION impact_records_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'An impact reference version or result never changes'; "
        "END $$"
    )
    op.execute(
        "CREATE TRIGGER impact_reference_versions_never_change BEFORE UPDATE OR DELETE ON "
        "impact_reference_versions FOR EACH ROW EXECUTE FUNCTION impact_records_never_change()"
    )
    op.execute(
        "CREATE TRIGGER impact_results_never_change BEFORE UPDATE OR DELETE ON "
        "impact_results FOR EACH ROW EXECUTE FUNCTION impact_records_never_change()"
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
    enable_tenant_isolation(op.get_bind(), "impact_references")
    enable_tenant_isolation(op.get_bind(), "impact_reference_versions")
    enable_tenant_isolation(op.get_bind(), "impact_results")
    enable_tenant_isolation(op.get_bind(), "idempotency_keys")
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
