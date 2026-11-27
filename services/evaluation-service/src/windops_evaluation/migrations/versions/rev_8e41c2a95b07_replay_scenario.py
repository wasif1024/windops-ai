"""Record which scenario's recorded external answer a running job is replaying (EVL-001, M12.6).

Revision ID: 8e41c2a95b07
"""

import sqlalchemy as sa
from alembic import op

revision: str = "8e41c2a95b07"
down_revision: str | None = "5a773b7e1d73"
# "expand" keeps the database working for the running version (DAT-001): the column has a
# default, so the version before this one keeps writing jobs.
phase: str = "expand"


def upgrade() -> None:
    op.add_column(
        "evaluation_jobs",
        sa.Column("replay_scenario", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("evaluation_jobs", "replay_scenario")
