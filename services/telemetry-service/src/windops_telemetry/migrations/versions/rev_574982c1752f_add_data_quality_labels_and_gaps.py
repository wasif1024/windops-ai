"""Add what data-quality detection needs: arrival order, readable rejections, counts and gaps.

Revision ID: 574982c1752f
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "574982c1752f"
down_revision: str | None = "a4673f912855"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Every new column may be empty, so the running version, which does not write them, works.
    # On a partitioned table, each is added to every partition too.
    op.add_column("operational_samples", sa.Column("arrival_index", sa.Integer()))
    op.add_column("ingest_rejections", sa.Column("sequence", sa.BigInteger()))
    op.add_column("ingest_rejections", sa.Column("event_time", sa.DateTime(timezone=True)))
    op.create_index(
        "ingest_rejections_by_sequence",
        "ingest_rejections",
        ["tenant_id", "turbine_id", "sequence"],
    )
    for name in (
        "delayed_samples",
        "out_of_order_samples",
        "missing_samples",
        "malformed_samples",
    ):
        op.add_column("scada_summaries", sa.Column(name, sa.Integer()))
    op.create_table(
        "data_gaps",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), primary_key=True),
        sa.Column("period_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("first_sequence", sa.BigInteger(), primary_key=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("missing", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
    )
    enable_tenant_isolation(op.get_bind(), "data_gaps")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
