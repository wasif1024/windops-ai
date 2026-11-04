#!/usr/bin/env python3
"""Prove migration Jobs work on the real local cluster, using a throwaway service and database.

Run `make secrets` and `make infra` first. The service runs from a temporary copy of the repository,
so the repository does not change, and its image, database, login and Secret are removed afterwards.
"""

import json
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread

import yaml
from provision_postgres import ADMIN_USER, HOST, POD, PORT, database_name, provisioning_sql
from provision_postgres import NAMESPACE as DATA_NAMESPACE
from run_migrations import MigrationError, run_job
from verify_cluster import IGNORED, kubectl, minikube_docker_env, run

REPOSITORY = Path(__file__).resolve().parent.parent
DIRECTORY = "migrate-probe-service"
SHORT = "migrate-probe"
PACKAGE = "windops_migrate_probe"
IMAGE = "windops-migrate-probe:local"
NAMESPACE = "windops-application"
DATABASE = database_name(SHORT)
SECRET = f"windops-{SHORT}-database"
JOB = "windops-migrate-probe-migrate"


def revision(identifier: str, parent: str | None, body: str, phase: str = "expand") -> str:
    return f'''"""A revision for the check."""

import sqlalchemy as sa
from alembic import op

revision: str = "{identifier}"
down_revision: str | None = {parent!r}
phase: str = "{phase}"


def upgrade() -> None:
    {body}


def downgrade() -> None:
    raise NotImplementedError
'''


CREATE = revision(
    "aaa1",
    None,
    'op.create_table("probe_items", sa.Column("note", sa.Text()))\n'
    "    op.execute(\"INSERT INTO probe_items VALUES ('first')\")",
)
# Creates a table first, to prove the failure undoes it.
BROKEN = revision(
    "bbb2",
    "aaa1",
    'op.create_table("probe_partial", sa.Column("note", sa.Text()))\n'
    '    op.execute("SELECT 1 / 0")',
)
# Slow, and adds one row, so two runs at once would leave two rows.
SLOW = revision(
    "ccc3",
    "aaa1",
    'op.execute("SELECT pg_sleep(4)")\n    op.execute("INSERT INTO probe_items VALUES (\'once\')")',
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def admin_sql(database: str, sql: str) -> str:
    return kubectl(
        "exec", POD, f"--namespace={DATA_NAMESPACE}", "--", "psql", "--no-psqlrc", "--tuples-only",
        "--no-align", "--set=ON_ERROR_STOP=1", f"--username={ADMIN_USER}", f"--dbname={database}",
        f"--command={sql}",
    ).stdout.strip()  # fmt: skip


def create_database() -> None:
    password = secrets.token_urlsafe(32)
    kubectl(
        "exec", "--stdin", POD, f"--namespace={DATA_NAMESPACE}", "--", "psql", "--no-psqlrc",
        "--quiet", "--set=ON_ERROR_STOP=1", f"--username={ADMIN_USER}", "--dbname=postgres",
        stdin=provisioning_sql({SHORT: password}),
    )  # fmt: skip
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": SECRET, "namespace": NAMESPACE},
        "type": "Opaque",
        "stringData": {
            "host": HOST,
            "port": PORT,
            "database": DATABASE,
            "username": DATABASE,
            "password": password,
        },
    }
    kubectl("apply", "--filename=-", stdin=json.dumps(manifest))


def remove_everything() -> None:
    kubectl(
        "delete", "job", JOB, "second-probe-migrate", f"--namespace={NAMESPACE}",
        "--ignore-not-found", "--wait=true", check=False,
    )  # fmt: skip
    kubectl(
        "delete", "secret", SECRET, f"--namespace={NAMESPACE}", "--ignore-not-found", check=False
    )
    for sql in (
        f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)",
        f"DROP ROLE IF EXISTS {DATABASE}",
    ):
        kubectl(
            "exec", POD, f"--namespace={DATA_NAMESPACE}", "--", "psql", "--no-psqlrc",
            f"--username={ADMIN_USER}", "--dbname=postgres", f"--command={sql}",
            check=False,
        )  # fmt: skip
    run("docker", "rmi", "-f", IMAGE, env=minikube_docker_env(), check=False)


def build_with(root: Path, *revisions: tuple[str, str]) -> None:
    """Build the probe's image holding exactly these revisions."""
    versions = root / "services" / DIRECTORY / "src" / PACKAGE / "migrations" / "versions"
    for existing in versions.glob("rev_*.py"):
        existing.unlink()
    for name, text in revisions:
        (versions / f"rev_{name}.py").write_text(text)
    run(
        "docker", "build", "-q", "-f", f"services/{DIRECTORY}/Dockerfile", "-t", IMAGE, ".",
        cwd=root, env=minikube_docker_env(),
    )  # fmt: skip


def applied_version() -> str:
    return admin_sql(DATABASE, "SELECT version_num FROM alembic_version")


def table_exists(name: str) -> bool:
    return admin_sql(DATABASE, f"SELECT to_regclass('public.{name}') IS NOT NULL") == "t"


def rows() -> str:
    return admin_sql(DATABASE, "SELECT count(*) FROM probe_items")


def check_first_run(manifest: str) -> None:
    run_job(manifest)
    check(applied_version() == "aaa1", "the Job applies a service's migration")
    check(rows() == "1", "the migration's data is in that service's own database")
    owner = admin_sql(DATABASE, "SELECT tableowner FROM pg_tables WHERE tablename = 'probe_items'")
    check(owner == DATABASE, "the service's own login owns its tables (DAT-001)")


def check_second_run(manifest: str) -> None:
    run_job(manifest)
    check(
        applied_version() == "aaa1" and rows() == "1",
        "running the Job again changes nothing, so a repeat is safe",
    )


def check_failure(manifest: str) -> None:
    failed = False
    try:
        run_job(manifest)
    except MigrationError as error:
        failed = "division by zero" in str(error).lower()
    check(failed, "a failed migration fails the Job, and its log says why")
    check(applied_version() == "aaa1", "a failed migration does not move the version")
    check(not table_exists("probe_partial"), "a failed migration undoes its partial work")
    pods = kubectl(
        "get", "pods", f"--namespace={NAMESPACE}", f"--selector=job-name={JOB}",
        "--no-headers", "--output=name",
    ).stdout.split()  # fmt: skip
    check(len(pods) == 1, "a failed Job is not retried, so a person sees it")


def check_concurrent_jobs(manifest: str) -> None:
    document = yaml.safe_load(manifest)
    document["metadata"]["name"] = "second-probe-migrate"
    document["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] = "second-probe"
    second = yaml.safe_dump(document)
    errors: list[Exception] = []

    def run_second() -> None:
        try:
            run_job(second)
        except MigrationError as error:
            errors.append(error)

    thread = Thread(target=run_second)
    thread.start()
    run_job(manifest)
    thread.join()
    check(not errors, "two Jobs at once both finish")
    check(
        applied_version() == "ccc3" and rows() == "2",
        "two Jobs at once apply a migration only once",
    )


def main() -> int:
    code = 0
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "repository"
        shutil.copytree(REPOSITORY, root, ignore=IGNORED)
        try:
            run("make", "service", f"NAME={DIRECTORY}", "DATABASE=yes", cwd=root)
            job_path = root / "services" / DIRECTORY / "deploy" / "migrate-job.yaml"
            manifest = job_path.read_text()
            create_database()
            build_with(root, ("aaa1", CREATE))
            check_first_run(manifest)
            check_second_run(manifest)
            build_with(root, ("aaa1", CREATE), ("bbb2", BROKEN))
            check_failure(manifest)
            build_with(root, ("aaa1", CREATE), ("ccc3", SLOW))
            check_concurrent_jobs(manifest)
        except (RuntimeError, MigrationError, subprocess.CalledProcessError) as error:
            print(error, file=sys.stderr)
            code = 1
        finally:
            remove_everything()
    if code == 0:
        print("Migration check passed")
    return code


if __name__ == "__main__":
    sys.exit(main())
