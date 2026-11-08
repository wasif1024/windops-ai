"""Add CMS vibration features, partitioned by month, and raw CMS windows, partitioned by day.

Revision ID: a4673f912855
"""

from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "a4673f912855"
down_revision: str | None = "2bb555121ac4"
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's list of features, so this revision never changes.
    op.execute(
        "CREATE TABLE cms_features ("
        "tenant_id text NOT NULL, "
        "turbine_id text NOT NULL, "
        "sensor text NOT NULL, "
        "event_time timestamptz NOT NULL, "
        "sequence bigint NOT NULL, "
        "device_id text NOT NULL, "
        "ingested_at timestamptz NOT NULL, "
        "source text NOT NULL, "
        "schema_version text NOT NULL, "
        "model_version text NOT NULL, "
        "synthetic boolean NOT NULL, "
        "feature_version text NOT NULL, "
        "sample_rate_hz integer NOT NULL, "
        "samples integer NOT NULL, "
        "rotor_speed_rpm double precision NOT NULL, "
        "generator_speed_rpm double precision NOT NULL, "
        "shaft_hz double precision NOT NULL, "
        "quality_status text NOT NULL, "
        "window_id text, "
        "trigger text, "
        "rms double precision, "
        "peak double precision, "
        "crest_factor double precision, "
        "kurtosis double precision, "
        "band_shaft_rms double precision, "
        "band_bearing_rms double precision, "
        "band_gear_mesh_rms double precision, "
        "band_broadband_rms double precision, "
        "band_high_rms double precision, "
        "PRIMARY KEY (tenant_id, turbine_id, sensor, event_time)"
        ") PARTITION BY RANGE (event_time)"
    )
    op.execute("CREATE TABLE cms_features_default PARTITION OF cms_features DEFAULT")
    op.execute(
        "CREATE TABLE cms_waveforms ("
        "tenant_id text NOT NULL, "
        "turbine_id text NOT NULL, "
        "sensor text NOT NULL, "
        "event_time timestamptz NOT NULL, "
        "window_id text NOT NULL, "
        "ingested_at timestamptz NOT NULL, "
        "sample_rate_hz integer NOT NULL, "
        "samples integer NOT NULL, "
        "unit text NOT NULL, "
        "kept_for text NOT NULL, "
        "values_f32le bytea NOT NULL, "
        "PRIMARY KEY (tenant_id, turbine_id, sensor, event_time)"
        ") PARTITION BY RANGE (event_time)"
    )
    op.execute("CREATE TABLE cms_waveforms_default PARTITION OF cms_waveforms DEFAULT")
    connection = op.get_bind()
    for table in ("cms_features", "cms_features_default", "cms_waveforms", "cms_waveforms_default"):
        enable_tenant_isolation(connection, table)


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
