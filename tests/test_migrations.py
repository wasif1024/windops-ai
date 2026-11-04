"""Hold every service's migrations to DAT-001 and DAT-002."""

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from windops_migrations import find_violations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from new_service import create_service, template_values
from provision_postgres import SERVICE_NAMESPACES
from run_migrations import job_manifests

ROOT = Path(__file__).resolve().parent.parent
REVISIONS = sorted(ROOT.glob("services/*/src/*/migrations/versions/rev_*.py"))
HISTORIES = sorted(ROOT.glob("services/*/src/*/migrations"))


@pytest.mark.parametrize("path", REVISIONS, ids=lambda path: path.name)
def test_every_revision_follows_expand_and_contract(path: Path) -> None:
    assert [str(item) for item in find_violations(path)] == []


@pytest.mark.parametrize("history", HISTORIES, ids=lambda path: path.parent.name)
def test_every_history_has_one_head(history: Path) -> None:
    """Two heads would mean two people's migrations with no agreed order."""
    config = Config()
    config.set_main_option("script_location", str(history))
    assert len(ScriptDirectory.from_config(config).get_heads()) <= 1


def job(service: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((service / "deploy" / "migrate-job.yaml").read_text())
    return document


@pytest.fixture
def service(tmp_path: Path) -> Path:
    return create_service("telemetry-service", tmp_path)


def test_a_service_that_stores_data_gets_migrations(service: Path) -> None:
    package = service / "src" / "windops_telemetry" / "migrations"
    assert (package / "env.py").exists()
    assert (package / "script.py.mako").exists()
    assert (package / "versions").is_dir()
    assert '"windops-migrations"' in (service / "pyproject.toml").read_text()


@pytest.mark.parametrize("short", sorted(SERVICE_NAMESPACES))
def test_every_service_with_a_database_gets_a_job(short: str, tmp_path: Path) -> None:
    created = create_service(f"{short}-service", tmp_path)
    secret = job(created)["spec"]["template"]["spec"]["containers"][0]["env"][0]
    assert secret["valueFrom"]["secretKeyRef"]["name"] == f"windops-{short}-database"
    assert job(created)["metadata"]["namespace"] == SERVICE_NAMESPACES[short]


def test_a_service_without_a_database_gets_no_migrations(tmp_path: Path) -> None:
    created = create_service("web-bff-service", tmp_path)
    assert not (created / "deploy" / "migrate-job.yaml").exists()
    assert not (created / "src" / "windops_web_bff" / "migrations").exists()
    assert "windops-migrations" not in (created / "pyproject.toml").read_text()


def test_a_database_can_be_requested_for_any_service(tmp_path: Path) -> None:
    created = create_service("probe-service", tmp_path, database=True)
    assert (created / "deploy" / "migrate-job.yaml").exists()


def test_the_job_is_not_part_of_the_service_rollout(service: Path) -> None:
    """DAT-002: `make services` must never run a migration."""
    kustomization = yaml.safe_load((service / "deploy" / "kustomization.yaml").read_text())
    assert "migrate-job.yaml" not in kustomization["resources"]


def test_the_service_never_migrates_on_startup(service: Path) -> None:
    """DAT-002."""
    source = service / "src" / "windops_telemetry" / "main.py"
    assert "migrat" not in source.read_text().lower()
    assert "alembic" not in source.read_text().lower()
    assert "windops_migrations" not in (service / "Dockerfile").read_text()


def test_the_job_fails_visibly_and_does_not_retry(service: Path) -> None:
    spec = job(service)["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["template"]["spec"]["restartPolicy"] == "Never"
    assert spec["activeDeadlineSeconds"] > 0


def test_the_job_runs_the_services_own_migrations(service: Path) -> None:
    container = job(service)["spec"]["template"]["spec"]["containers"][0]
    values = template_values("telemetry-service")
    assert container["image"] == f"{values['workload']}:local"
    assert container["command"] == [
        "python",
        "-m",
        "windops_migrations",
        "upgrade",
        values["package"],
    ]


def test_the_job_meets_pod_security_and_has_limits(service: Path) -> None:
    pod = job(service)["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    container = pod["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert "memory" in container["resources"]["limits"]


def test_the_job_reads_its_password_from_a_secret(service: Path) -> None:
    env = job(service)["spec"]["template"]["spec"]["containers"][0]["env"]
    assert all("secretKeyRef" in item["valueFrom"] for item in env)


def test_jobs_are_found_in_a_fixed_order(tmp_path: Path) -> None:
    for name in ("telemetry-service", "alarm-service"):
        create_service(name, tmp_path)
    names = [path.parts[-3] for path in job_manifests(tmp_path)]
    assert names == ["alarm-service", "telemetry-service"]
