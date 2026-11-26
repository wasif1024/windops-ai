"""Create datasets, evaluation jobs and their attempts, benchmarks and idempotency keys, by tenant,
and the outbox.

Revision ID: 5a773b7e1d73
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation
from windops_outbox import create_outbox_table

revision: str = "5a773b7e1d73"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "datasets",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("scenario_count", sa.Integer(), nullable=False),
        sa.Column("registered_by", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "dataset_scenarios",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("dataset_name", sa.Text(), primary_key=True),
        sa.Column("dataset_version", sa.Text(), primary_key=True),
        sa.Column("scenario_id", sa.Text(), primary_key=True),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("runs_required", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.CheckConstraint("split IN ('train', 'validation', 'held_out')", name="scenario_split"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dataset_name", "dataset_version"],
            ["datasets.tenant_id", "datasets.name", "datasets.version"],
            name="scenario_dataset",
        ),
    )
    # A registered dataset version never changes, so a result always names what it scored
    # (EVL-005).
    op.execute(
        "CREATE FUNCTION datasets_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A registered dataset version never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER datasets_never_change BEFORE UPDATE OR DELETE ON datasets "
        "FOR EACH ROW EXECUTE FUNCTION datasets_never_change()"
    )
    op.execute(
        "CREATE TRIGGER dataset_scenarios_never_change BEFORE UPDATE OR DELETE ON "
        "dataset_scenarios FOR EACH ROW EXECUTE FUNCTION datasets_never_change()"
    )
    op.create_table(
        "evaluation_jobs",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("dataset_name", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("splits", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runner", sa.Text(), nullable=False, server_default=""),
        sa.Column("lease_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("versions_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint("mode IN ('rehearse', 'claude')", name="job_mode"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'passed', 'failed', 'incomplete', 'cancelled')",
            name="job_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dataset_name", "dataset_version"],
            ["datasets.tenant_id", "datasets.name", "datasets.version"],
            name="job_dataset",
        ),
    )
    op.create_index("jobs_by_status", "evaluation_jobs", ["tenant_id", "status", "requested_at"])
    op.create_index(
        "jobs_by_dataset", "evaluation_jobs", ["tenant_id", "dataset_name", "requested_at"]
    )
    op.create_table(
        "evaluation_attempts",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("scenario_id", sa.Text(), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("case_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("rehearsed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scores", sa.LargeBinary()),
        sa.Column("seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("reported_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('planned', 'passed', 'failed', 'skipped', 'error')", name="attempt_status"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["evaluation_jobs.tenant_id", "evaluation_jobs.job_id"],
            name="attempt_job",
        ),
    )
    # A reported attempt is kept as reported: only a planned one changes, and only once.
    op.execute(
        "CREATE FUNCTION attempts_reported_once() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN IF TG_OP = 'DELETE' OR OLD.status <> 'planned' THEN "
        "RAISE EXCEPTION 'A reported attempt never changes'; END IF; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER attempts_reported_once BEFORE UPDATE OR DELETE ON evaluation_attempts "
        "FOR EACH ROW EXECUTE FUNCTION attempts_reported_once()"
    )
    op.create_table(
        "benchmarks",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("dataset_name", sa.Text(), primary_key=True),
        sa.Column("splits", sa.Text(), primary_key=True),
        sa.Column("mode", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["evaluation_jobs.tenant_id", "evaluation_jobs.job_id"],
            name="benchmark_job",
        ),
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
    for table in (
        "datasets",
        "dataset_scenarios",
        "evaluation_jobs",
        "evaluation_attempts",
        "benchmarks",
        "idempotency_keys",
    ):
        enable_tenant_isolation(op.get_bind(), table)
    create_outbox_table(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
