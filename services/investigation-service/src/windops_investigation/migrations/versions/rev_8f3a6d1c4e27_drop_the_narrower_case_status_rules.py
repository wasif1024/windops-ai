"""Drop the status constraint and index that leave out "approved", now wider ones stand beside them.

Revision ID: 8f3a6d1c4e27
"""

from alembic import op

revision: str = "8f3a6d1c4e27"
down_revision: str | None = "5b1e7c2a9d40"
# "contract": it drops a constraint and an index. The version before M10.4 never writes
# "approved", and the revision before holds every other status and active case as these did.
phase: str = "contract"


def upgrade() -> None:
    op.drop_constraint("cases_status", "cases", type_="check")
    op.drop_index("cases_one_active", table_name="cases")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
