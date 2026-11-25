"""Drop the status rule and index that leave out verification, now wider ones stand beside them.

Revision ID: 0d9e3b6a5f18
"""

from alembic import op

revision: str = "0d9e3b6a5f18"
down_revision: str | None = "6c4f1a8e2b57"
# "contract": it drops a constraint and an index. The version before M10.6 never writes
# "verifying" or "verified", and the revision before holds every other status and active case.
phase: str = "contract"


def upgrade() -> None:
    op.drop_constraint("cases_status_with_approved", "cases", type_="check")
    op.drop_index("cases_one_active_or_approved", table_name="cases")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
