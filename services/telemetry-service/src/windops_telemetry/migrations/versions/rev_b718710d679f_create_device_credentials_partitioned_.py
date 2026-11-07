"""Create device credentials, samples partitioned by day, rejections and idempotency keys, isolated
by tenant, and the outbox.

Revision ID: b718710d679f
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "b718710d679f"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    op.create_table(
        "device_credentials",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("device_id", sa.Text(), primary_key=True),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "device_credentials_by_turbine", "device_credentials", ["tenant_id", "turbine_id"]
    )
    # Partitioned by event time, a day each (ADR 0003). The service makes each day's partition
    # ahead of time, and the default partition holds anything outside them, so nothing is lost.
    # Written out, not built from the code's list of readings, so this revision never changes.
    # A later reading is a new revision's nullable column.
    op.execute(
        "CREATE TABLE operational_samples ("
        "tenant_id text NOT NULL, "
        "turbine_id text NOT NULL, "
        "event_time timestamptz NOT NULL, "
        "sequence bigint NOT NULL, "
        "device_id text NOT NULL, "
        "ingested_at timestamptz NOT NULL, "
        "source text NOT NULL, "
        "schema_version text NOT NULL, "
        "model_version text NOT NULL, "
        "synthetic boolean NOT NULL, "
        "operating_state text NOT NULL, "
        "available boolean NOT NULL, "
        "quality_status text NOT NULL, "
        "wind_speed_mps double precision, "
        "wind_direction_deg double precision, "
        "rotor_speed_rpm double precision, "
        "generator_speed_rpm double precision, "
        "power_kw double precision, "
        "pitch_angle_deg double precision, "
        "ambient_temp_c double precision, "
        "gearbox_oil_temp_c double precision, "
        "gearbox_bearing_temp_c double precision, "
        "main_bearing_temp_c double precision, "
        "generator_bearing_temp_c double precision, "
        "nacelle_vibration_mm_s double precision"
        ") PARTITION BY RANGE (event_time)"
    )
    op.execute("CREATE TABLE operational_samples_default PARTITION OF operational_samples DEFAULT")
    op.create_index(
        "operational_samples_by_turbine",
        "operational_samples",
        ["tenant_id", "turbine_id", "event_time"],
    )
    op.create_table(
        "ingest_rejections",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("rejection_id", sa.Text(), primary_key=True),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("turbine_id", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
    )
    op.create_index(
        "ingest_rejections_by_turbine",
        "ingest_rejections",
        ["tenant_id", "turbine_id", "received_at"],
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    connection = op.get_bind()
    # The partitions too, so even a query that names one directly sees only its tenant's rows.
    for table in (
        "device_credentials",
        "operational_samples",
        "operational_samples_default",
        "ingest_rejections",
        "idempotency_keys",
    ):
        enable_tenant_isolation(connection, table)
    create_outbox_table(connection)


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
