"""Drop the status constraint that forbids "paused", now that a wider one stands beside it.

Revision ID: a04c9224d8d0
"""

from alembic import op

revision: str = "a04c9224d8d0"
down_revision: str | None = "34322b04af91"
# "contract": it drops a constraint. The version before M8.4 never writes "paused", and
# runs_status_with_paused, from the revision before, still holds every other status.
phase: str = "contract"


def upgrade() -> None:
    op.drop_constraint("runs_status", "runs", type_="check")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
