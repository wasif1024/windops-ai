"""Keep each investigation's checked, structured result on its run (M8.8, SRS 5.7).

Revision ID: 7626c37bc856
"""

import sqlalchemy as sa
from alembic import op

revision: str = "7626c37bc856"
down_revision: str | None = "53b66c007cc5"
# "expand": a column with a default, which the version before this one never reads.
phase: str = "expand"


def upgrade() -> None:
    op.add_column("runs", sa.Column("result_json", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
