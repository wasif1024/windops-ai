"""Create approval requests and their decisions, and idempotency keys, by tenant, and the outbox.

Revision ID: 4c770b1d4d84
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "4c770b1d4d84"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "approvals",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("approval_id", sa.Text(), primary_key=True),
        sa.Column("work_order_id", sa.Text(), nullable=False),
        sa.Column("proposal_version", sa.BigInteger(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("submitted_by", sa.Text(), nullable=False),
        sa.Column("drafted_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("drafted_by_agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.LargeBinary()),
        sa.Column("decided_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'superseded')", name="approval_status"
        ),
        # One request a proposal version, however often its event arrives (EVT-003).
        sa.UniqueConstraint(
            "tenant_id", "work_order_id", "proposal_version", name="one_request_a_version"
        ),
    )
    op.create_index("approvals_by_status", "approvals", ["tenant_id", "status", "requested_at"])
    # A decision, or a supersession, is kept as it was made: never changed, never deleted
    # (FR-APR-002). Only a pending request changes, and only once.
    op.execute(
        "CREATE FUNCTION approval_decisions_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN IF TG_OP = 'DELETE' OR OLD.status <> 'pending' THEN "
        "RAISE EXCEPTION 'A decided or superseded approval never changes'; END IF; "
        "RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER approval_decisions_never_change BEFORE UPDATE OR DELETE ON approvals "
        "FOR EACH ROW EXECUTE FUNCTION approval_decisions_never_change()"
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("caller", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # DAT-005. The outbox is not here: only the relay reads it, never a request.
    for table in ("approvals", "idempotency_keys"):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
