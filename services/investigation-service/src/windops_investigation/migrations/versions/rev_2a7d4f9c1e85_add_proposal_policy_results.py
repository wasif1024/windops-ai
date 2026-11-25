"""Keep each proposal's Policy result at submission, for the weather rule (M10.3b).

Revision ID: 2a7d4f9c1e85
"""

import sqlalchemy as sa
from alembic import op

revision: str = "2a7d4f9c1e85"
down_revision: str | None = "0d9e3b6a5f18"
# "expand" keeps the database working for the running version (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    for name in ("policy_result_id", "impact_result_id"):
        op.add_column(
            "case_proposals", sa.Column(name, sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
