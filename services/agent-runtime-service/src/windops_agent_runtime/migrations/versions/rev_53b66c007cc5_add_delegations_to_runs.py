"""Name the delegation each run's agents act under (M8.5, ADR 0019).

Revision ID: 53b66c007cc5
"""

import sqlalchemy as sa
from alembic import op

revision: str = "53b66c007cc5"
down_revision: str | None = "a04c9224d8d0"
# "expand": a column with a default, which the version before this one never reads.
phase: str = "expand"


def upgrade() -> None:
    op.add_column("runs", sa.Column("delegation_id", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
