"""Add what was done, once a work order is completed, and its verification's outcome (M10.6).

Revision ID: 3e8b5c1f7a92
"""

import sqlalchemy as sa
from alembic import op

revision: str = "3e8b5c1f7a92"
down_revision: str | None = "41d06bcd24a7"
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.add_column("work_orders", sa.Column("completion", sa.LargeBinary(), nullable=True))
    op.add_column(
        "work_orders",
        sa.Column("verification_outcome", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
