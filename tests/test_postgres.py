"""Check the PostgreSQL manifests and provisioning against SRS sections 8 and 11."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from local_secrets import ensure_passwords, postgres_secrets
from provision_postgres import (
    ADMIN_KEY,
    SERVICE_NAMESPACES,
    database_name,
    provisioning_sql,
    read_passwords,
    redact,
)

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "infrastructure" / "kubernetes" / "base" / "data"
TEMPLATE_PASSWORDS = {key: f"password-{index}" for index, key in enumerate(SERVICE_NAMESPACES)}


@pytest.fixture(scope="module")
def documents() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(STACK)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def statefulset(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        item
        for item in documents
        if item["kind"] == "StatefulSet" and item["metadata"]["name"] == "windops-postgres"
    )


def test_everything_is_in_the_data_namespace(documents: list[dict[str, Any]]) -> None:
    for item in documents:
        assert item["metadata"]["namespace"] == "windops-data"


def test_postgres_is_not_exposed_outside_the_cluster(documents: list[dict[str, Any]]) -> None:
    """DEP-007."""
    service = next(
        item
        for item in documents
        if item["kind"] == "Service" and item["metadata"]["name"] == "windops-postgres"
    )
    assert service["spec"]["type"] == "ClusterIP"


def test_postgres_keeps_data_on_a_persistent_volume(documents: list[dict[str, Any]]) -> None:
    """DEP-006."""
    workload = statefulset(documents)
    claim = workload["spec"]["volumeClaimTemplates"][0]["metadata"]["name"]
    mounts = workload["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {"name": claim, "mountPath": "/var/lib/postgresql"} in mounts


def test_postgres_is_pinned_probed_and_limited(documents: list[dict[str, Any]]) -> None:
    """DEP-004 and K8S-007."""
    container = statefulset(documents)["spec"]["template"]["spec"]["containers"][0]
    assert "@sha256:" in container["image"]
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(container)
    assert {"cpu", "memory"} <= set(container["resources"]["limits"])


def test_postgres_meets_the_restricted_pod_security_profile(
    documents: list[dict[str, Any]],
) -> None:
    pod = statefulset(documents)["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    context = pod["containers"][0]["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"]["drop"] == ["ALL"]


def test_network_logins_need_a_password(documents: list[dict[str, Any]]) -> None:
    container = statefulset(documents)["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["POSTGRES_HOST_AUTH_METHOD"]["value"] == "scram-sha-256"
    assert "--auth-host=scram-sha-256" in env["POSTGRES_INITDB_ARGS"]["value"]
    assert "secretKeyRef" in env["POSTGRES_PASSWORD"]["valueFrom"]


def test_no_time_series_extension_is_used(documents: list[dict[str, Any]]) -> None:
    """ADR 0003."""
    assert "timescale" not in json.dumps(documents).lower()


def test_sql_gives_each_service_its_own_private_database() -> None:
    """ARC-002 and ARC-003."""
    sql = provisioning_sql(TEMPLATE_PASSWORDS)
    for service in SERVICE_NAMESPACES:
        name = database_name(service)
        assert f"CREATE DATABASE {name} OWNER {name}" in sql
        assert f"REVOKE ALL ON DATABASE {name} FROM PUBLIC;" in sql
    for shared in ("postgres", "template1", "windops_admin"):
        assert f"REVOKE ALL ON DATABASE {shared} FROM PUBLIC;" in sql


def test_sql_gives_service_logins_no_extra_powers() -> None:
    sql = provisioning_sql(TEMPLATE_PASSWORDS)
    grants = [line for line in sql.splitlines() if line.startswith("ALTER ROLE")]
    assert len(grants) == len(SERVICE_NAMESPACES)
    for line in grants:
        for power in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS"):
            assert power in line


def test_sql_can_run_repeatedly() -> None:
    sql = provisioning_sql(TEMPLATE_PASSWORDS)
    for line in sql.splitlines():
        if line.startswith("SELECT 'CREATE"):
            assert "WHERE NOT EXISTS" in line


def test_sql_turns_off_statement_logging_before_any_password() -> None:
    sql = provisioning_sql(TEMPLATE_PASSWORDS)
    assert sql.index("log_statement = 'none'") < sql.index("PASSWORD")
    assert sql.index("log_min_error_statement = 'panic'") < sql.index("PASSWORD")


@pytest.mark.parametrize("password", ["it's", "a b", "x;DROP", "a\\b", ""])
def test_sql_rejects_a_password_that_could_change_the_statement(password: str) -> None:
    with pytest.raises(ValueError, match="audit"):
        provisioning_sql({"audit": password})


@pytest.mark.parametrize("service", ["Audit", "a b", "audit;DROP", "audit'", "-audit", ""])
def test_sql_rejects_a_service_name_that_could_change_the_statement(service: str) -> None:
    with pytest.raises(ValueError, match="Unsupported service name"):
        provisioning_sql({service: "valid-password"})


def test_error_text_never_contains_a_password() -> None:
    message = "LINE 1: ALTER ROLE windops_audit PASSWORD 'hunter2-value';"
    assert "hunter2-value" not in redact(message, ["hunter2-value"])


def test_each_secret_reaches_only_its_service_namespace() -> None:
    """ARC-003: distinct credentials, and no administrator password for services."""
    passwords = {ADMIN_KEY: "admin-password", "keycloak": "kc-value", **TEMPLATE_PASSWORDS}
    manifests = postgres_secrets(passwords)
    assert len(manifests) == len(SERVICE_NAMESPACES) + 2
    by_name: dict[str, Any] = {item["metadata"]["name"]: item for item in manifests}  # type: ignore[index]
    assert by_name["windops-postgres-admin"]["metadata"]["namespace"] == "windops-data"
    for service, namespace in SERVICE_NAMESPACES.items():
        secret = by_name[f"windops-{service}-database"]
        assert secret["metadata"]["namespace"] == namespace
        assert secret["stringData"]["password"] == passwords[service]
        assert secret["stringData"]["username"] == database_name(service)
        assert "admin-password" not in json.dumps(secret)


def test_service_passwords_are_all_different(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    ensure_passwords(path, [ADMIN_KEY, *SERVICE_NAMESPACES])
    passwords = read_passwords(path)
    assert len(set(passwords.values())) == len(SERVICE_NAMESPACES) + 1


def test_password_file_keeps_existing_values_and_adds_new_ones(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    assert ensure_passwords(path, ["audit"]) == ["audit"]
    original = read_passwords(path)["audit"]
    assert ensure_passwords(path, ["audit", "alarm"]) == ["alarm"]
    assert read_passwords(path)["audit"] == original
    assert path.stat().st_mode & 0o077 == 0
