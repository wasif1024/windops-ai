"""Add 10-minute SCADA summaries, partitioned by month, retention holds and summary watermarks.

Revision ID: 2bb555121ac4
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "2bb555121ac4"
down_revision: str | None = "b718710d679f"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # The summarizer finds new samples by when they arrived.
    op.create_index("operational_samples_by_ingestion", "operational_samples", ["ingested_at"])
    # Partitioned by month on the period (ADR 0003). The service makes each month's partition
    # ahead of time, and the default partition holds anything outside them.
    # Written out, not built from the code's list of readings, so this revision never changes.
    op.execute(
        "CREATE TABLE scada_summaries ("
        "tenant_id text NOT NULL, "
        "turbine_id text NOT NULL, "
        "period_start timestamptz NOT NULL, "
        "period_end timestamptz NOT NULL, "
        "sample_count integer NOT NULL, "
        "coverage double precision NOT NULL, "
        "quality_status text NOT NULL, "
        "duplicate_samples integer NOT NULL, "
        "late_samples integer NOT NULL, "
        "null_readings integer NOT NULL, "
        "available_fraction double precision NOT NULL, "
        "producing_fraction double precision NOT NULL, "
        "energy_kwh double precision, "
        "source text NOT NULL, "
        "schema_version text NOT NULL, "
        "model_version text NOT NULL, "
        "synthetic boolean NOT NULL, "
        "revision integer NOT NULL, "
        "first_computed_at timestamptz NOT NULL, "
        "computed_at timestamptz NOT NULL, "
        "wind_speed_mps_mean double precision, "
        "wind_speed_mps_min double precision, "
        "wind_speed_mps_max double precision, "
        "wind_speed_mps_std double precision, "
        "wind_direction_deg_mean double precision, "
        "wind_direction_deg_min double precision, "
        "wind_direction_deg_max double precision, "
        "wind_direction_deg_std double precision, "
        "rotor_speed_rpm_mean double precision, "
        "rotor_speed_rpm_min double precision, "
        "rotor_speed_rpm_max double precision, "
        "rotor_speed_rpm_std double precision, "
        "generator_speed_rpm_mean double precision, "
        "generator_speed_rpm_min double precision, "
        "generator_speed_rpm_max double precision, "
        "generator_speed_rpm_std double precision, "
        "power_kw_mean double precision, "
        "power_kw_min double precision, "
        "power_kw_max double precision, "
        "power_kw_std double precision, "
        "pitch_angle_deg_mean double precision, "
        "pitch_angle_deg_min double precision, "
        "pitch_angle_deg_max double precision, "
        "pitch_angle_deg_std double precision, "
        "ambient_temp_c_mean double precision, "
        "ambient_temp_c_min double precision, "
        "ambient_temp_c_max double precision, "
        "ambient_temp_c_std double precision, "
        "gearbox_oil_temp_c_mean double precision, "
        "gearbox_oil_temp_c_min double precision, "
        "gearbox_oil_temp_c_max double precision, "
        "gearbox_oil_temp_c_std double precision, "
        "gearbox_bearing_temp_c_mean double precision, "
        "gearbox_bearing_temp_c_min double precision, "
        "gearbox_bearing_temp_c_max double precision, "
        "gearbox_bearing_temp_c_std double precision, "
        "main_bearing_temp_c_mean double precision, "
        "main_bearing_temp_c_min double precision, "
        "main_bearing_temp_c_max double precision, "
        "main_bearing_temp_c_std double precision, "
        "generator_bearing_temp_c_mean double precision, "
        "generator_bearing_temp_c_min double precision, "
        "generator_bearing_temp_c_max double precision, "
        "generator_bearing_temp_c_std double precision, "
        "nacelle_vibration_mm_s_mean double precision, "
        "nacelle_vibration_mm_s_min double precision, "
        "nacelle_vibration_mm_s_max double precision, "
        "nacelle_vibration_mm_s_std double precision, "
        "PRIMARY KEY (tenant_id, turbine_id, period_start)"
        ") PARTITION BY RANGE (period_start)"
    )
    op.execute("CREATE TABLE scada_summaries_default PARTITION OF scada_summaries DEFAULT")
    op.create_table(
        "retention_holds",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("hold_id", sa.Text(), primary_key=True),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "retention_holds_live", "retention_holds", ["table_name", "starts_at", "ends_at"]
    )
    # Platform upkeep, not tenant data, so it has no tenant column.
    op.create_table(
        "summary_watermarks",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=False),
    )
    connection = op.get_bind()
    for table in ("scada_summaries", "scada_summaries_default", "retention_holds"):
        enable_tenant_isolation(connection, table)


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
