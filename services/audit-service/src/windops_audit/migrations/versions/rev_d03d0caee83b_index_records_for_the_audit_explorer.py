"""Index audit records by who acted, what they acted on, and the request, for the explorer (M11.6).

Revision ID: d03d0caee83b
"""

from alembic import op

revision: str = "d03d0caee83b"
down_revision: str | None = "b2c4d6e8f013"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"

# Each with the sequence last, so a filtered page reads in order from the index.
INDEXES = {
    "audit_records_actor": ["tenant_id", "actor_id", "sequence"],
    "audit_records_resource": ["tenant_id", "resource_id", "sequence"],
    "audit_records_correlation": ["tenant_id", "correlation_id", "sequence"],
    "audit_records_occurred": ["tenant_id", "occurred_at"],
}


def upgrade() -> None:
    for name, columns in INDEXES.items():
        op.create_index(name, "audit_records", columns)


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
