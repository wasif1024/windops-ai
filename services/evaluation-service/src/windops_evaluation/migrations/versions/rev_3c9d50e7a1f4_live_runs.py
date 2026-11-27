"""Keep each run of the live suite, by tenant (EVL-001, M12.6b).

Revision ID: 3c9d50e7a1f4
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "3c9d50e7a1f4"
down_revision: str | None = "8e41c2a95b07"
# "expand": a new table, which the version before this one never reads (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    op.create_table(
        "live_runs",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("live_tenant_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("triggered_by", sa.Text(), nullable=False),
    )
    enable_tenant_isolation(op.get_bind(), "live_runs")


def downgrade() -> None:
    op.drop_table("live_runs")
