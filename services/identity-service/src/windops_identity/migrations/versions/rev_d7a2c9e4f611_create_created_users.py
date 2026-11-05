"""Remember which user each create call made, and add the outbox.

Revision ID: d7a2c9e4f611
"""

import sqlalchemy as sa
from alembic import op
from windops_outbox import create_outbox_table

revision: str = "d7a2c9e4f611"
down_revision: str | None = None
phase: str = "expand"


def upgrade() -> None:
    # A retried create returns the same user (GRP-007). The password is never stored.
    op.create_table(
        "created_users",
        sa.Column("actor_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_outbox_table(op.get_bind())
