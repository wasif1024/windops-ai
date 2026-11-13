"""Add evidence snapshots and their items, which never change, and each case's newest.

Revision ID: 436ab17d1529
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "436ab17d1529"
down_revision: str | None = "f6d84e59d898"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.add_column("cases", sa.Column("evidence_snapshot_id", sa.Text(), nullable=False,
                                     server_default=""))  # fmt: skip
    op.add_column("cases", sa.Column("evidence_version", sa.BigInteger(), nullable=False,
                                     server_default="0"))  # fmt: skip
    op.add_column("cases", sa.Column("evidence_wanted_at", sa.DateTime(timezone=True)))
    op.create_table(
        "evidence_snapshots",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("hash_format", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_count", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "evidence_one_version", "evidence_snapshots", ["tenant_id", "case_id", "version"],
        unique=True,
    )  # fmt: skip
    op.create_table(
        "evidence_items",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), primary_key=True),
        sa.Column("source_service", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True)),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
    )
    # Frozen means frozen: not this service, nor anyone with its login, may change one (EVD-001).
    op.execute(
        "CREATE FUNCTION evidence_never_changes() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'An evidence snapshot never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER evidence_snapshots_never_change BEFORE UPDATE OR DELETE ON "
        "evidence_snapshots FOR EACH ROW EXECUTE FUNCTION evidence_never_changes()"
    )
    op.execute(
        "CREATE TRIGGER evidence_items_never_change BEFORE UPDATE OR DELETE ON "
        "evidence_items FOR EACH ROW EXECUTE FUNCTION evidence_never_changes()"
    )
    enable_tenant_isolation(op.get_bind(), "evidence_snapshots")
    enable_tenant_isolation(op.get_bind(), "evidence_items")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
