"""Create the outbox, from which the relay publishes this service's audit records (EVT-002).

The first revision left it out, and had run by then, so it comes in a revision of its own.

Revision ID: 2d2a3af57a9a
"""

from alembic import op
from windops_outbox import create_outbox_table

revision: str = "2d2a3af57a9a"
down_revision: str | None = "2f546b7c310f"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Not isolated by tenant: only the relay reads it, never a request.
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
