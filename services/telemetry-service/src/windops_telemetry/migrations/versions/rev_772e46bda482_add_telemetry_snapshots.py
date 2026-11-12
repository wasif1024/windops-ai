"""add telemetry snapshots

Revision ID: 772e46bda482

Snapshots of a turbine's window, frozen for investigations (FR-TEL-006, SRS 8.4), and the samples
each holds. Both are new tables, which the running version does not use.
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "772e46bda482"
down_revision: str | None = "574982c1752f"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    op.create_table(
        "telemetry_snapshots",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("from_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("to_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("ingested_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("manifest", sa.LargeBinary(), nullable=False),
        sa.Column("hold_id", sa.Text(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "telemetry_snapshots_by_turbine", "telemetry_snapshots", ["tenant_id", "turbine_id"]
    )
    enable_tenant_isolation(op.get_bind(), "telemetry_snapshots")
    op.create_table(
        "snapshot_samples",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrival_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
    )
    enable_tenant_isolation(op.get_bind(), "snapshot_samples")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
