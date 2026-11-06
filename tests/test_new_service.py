"""Check the service generator and the manifests it creates."""

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.dev.new_service import (
    ScaffoldError,
    add_workspace_member,
    create_service,
    template_values,
)


def manifests(service: Path) -> dict[str, dict[str, Any]]:
    documents = [
        yaml.safe_load(path.read_text())
        for path in (service / "deploy").glob("*.yaml")
        if path.name != "kustomization.yaml"
    ]
    return {document["kind"]: document for document in documents}


@pytest.fixture
def service(tmp_path: Path) -> Path:
    return create_service("telemetry-service", tmp_path)


def test_workspace_member_is_added_once_and_keeps_other_text() -> None:
    text = (
        'before\nmembers = ["shared/grpc"]\nfiles = [\n    "scripts",\n]\n'
        'mypy_path = [\n    "shared/grpc/src",\n]\nafter\n'
    )
    updated = add_workspace_member("telemetry-service", text)
    parsed = tomllib.loads(updated.replace("before\n", "").replace("after\n", ""))
    base = "services/telemetry-service"
    assert parsed["members"] == [base, "shared/grpc"]
    assert parsed["files"] == ["scripts", f"{base}/src", f"{base}/tests"]
    assert parsed["mypy_path"] == [f"{base}/src", f"{base}/tests", "shared/grpc/src"]
    assert updated.startswith("before\n")
    assert updated.endswith("after\n")
    assert add_workspace_member("telemetry-service", updated) == updated


def test_missing_workspace_list_is_an_error() -> None:
    with pytest.raises(ScaffoldError):
        add_workspace_member("telemetry-service", "no members here")


def test_names_follow_the_srs_conventions() -> None:
    values = template_values("telemetry-service")
    assert values["short"] == "telemetry"
    assert values["workload"] == "windops-telemetry"
    assert values["package"] == "windops_telemetry"


def test_hyphenated_name_makes_a_valid_python_package() -> None:
    values = template_values("fault-detection-service")
    assert values["package"] == "windops_fault_detection"
    assert values["workload"] == "windops-fault-detection"


def test_a_name_without_the_suffix_is_kept_whole() -> None:
    assert template_values("web-bff")["workload"] == "windops-web-bff"
    assert template_values("agent-sandbox-adapter")["short"] == "agent-sandbox-adapter"


@pytest.mark.parametrize(
    ("directory", "namespace"),
    [
        ("identity-service", "windops-system"),
        ("authorization-service", "windops-system"),
        ("tenant-service", "windops-system"),
        ("web-bff", "windops-system"),
        ("telemetry-service", "windops-application"),
        ("agent-runtime-service", "windops-application"),
    ],
)
def test_namespace_matches_srs_section_11_2(directory: str, namespace: str) -> None:
    assert template_values(directory)["namespace"] == namespace


@pytest.mark.parametrize(
    "name", ["Telemetry", "telemetry_service", "-bad", "bad-", "a/b", "../x", ""]
)
def test_invalid_names_are_rejected(name: str, tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError):
        create_service(name, tmp_path)


def test_api_gateway_is_refused_because_it_is_apisix(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError):
        create_service("api-gateway", tmp_path)


def test_every_required_file_is_created(service: Path) -> None:
    for relative in (
        "pyproject.toml",
        "README.md",
        "Dockerfile",
        "src/windops_telemetry/__init__.py",
        "src/windops_telemetry/main.py",
        "src/windops_telemetry/py.typed",
        "tests/test_windops_telemetry.py",
        "deploy/kustomization.yaml",
        "deploy/configmap.yaml",
        "deploy/deployment.yaml",
        "deploy/service.yaml",
        "deploy/pdb.yaml",
        "deploy/certificate.yaml",
    ):
        assert (service / relative).is_file(), relative


def test_no_placeholder_or_template_file_remains(service: Path) -> None:
    for path in service.rglob("*"):
        if path.is_file():
            assert "{{" not in path.read_text(), path
            assert path.suffix != ".tmpl", path


def test_existing_service_is_never_overwritten(service: Path) -> None:
    main = service / "src" / "windops_telemetry" / "main.py"
    main.write_text("# My work\n")
    with pytest.raises(ScaffoldError):
        create_service("telemetry-service", service.parent.parent)
    assert main.read_text() == "# My work\n"


def test_empty_placeholder_directory_is_replaced(tmp_path: Path) -> None:
    target = tmp_path / "services" / "telemetry-service"
    target.mkdir(parents=True)
    (target / ".gitkeep").touch()
    create_service("telemetry-service", tmp_path)
    assert not (target / ".gitkeep").exists()
    assert (target / "Dockerfile").exists()


def test_dockerfile_is_pinned_multi_stage_and_non_root(service: Path) -> None:
    lines = (service / "Dockerfile").read_text().splitlines()
    stages = [line for line in lines if line.startswith("FROM ")]
    assert len(stages) == 2
    assert all("@sha256:" in line for line in stages)
    assert "USER 10001:10001" in lines
    assert any(line.startswith("RUN uv sync --locked") for line in lines)
    assert not any(line.startswith(("ENV WINDOPS", "COPY .env")) for line in lines)
    runtime = lines[lines.index(stages[1]) :]
    assert any("pip uninstall" in line for line in runtime)


def test_a_service_gets_its_own_certificate_for_both_directions(service: Path) -> None:
    """GRP-005."""
    certificate = manifests(service)["Certificate"]
    spec = certificate["spec"]
    assert spec["commonName"] == "windops-telemetry"
    assert spec["secretName"] == "windops-telemetry-tls"
    assert "windops-telemetry.windops-application.svc.cluster.local" in spec["dnsNames"]
    assert set(spec["usages"]) == {"server auth", "client auth"}
    assert spec["issuerRef"]["name"] == "windops-internal"
    assert spec["privateKey"]["rotationPolicy"] == "Always"


def test_a_deployment_mounts_its_certificate_read_only(service: Path) -> None:
    pod = manifests(service)["Deployment"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["WINDOPS_TLS_DIR"] == "/var/run/windops/tls"
    mount = next(item for item in container["volumeMounts"] if item["name"] == "tls")
    assert mount["readOnly"] is True
    volume = next(item for item in pod["volumes"] if item["name"] == "tls")
    assert volume["secret"]["secretName"] == "windops-telemetry-tls"
    assert pod["securityContext"]["fsGroup"] == 10001


def test_deployment_meets_the_container_requirements(service: Path) -> None:
    deployment = manifests(service)["Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    # The kubelet has no client certificate, so a gRPC probe could never pass (GRP-005).
    assert container["startupProbe"]["httpGet"]["path"] == "/health/live"
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health/live"
    for section in ("requests", "limits"):
        assert set(container["resources"][section]) == {"cpu", "memory"}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    assert pod["automountServiceAccountToken"] is False


def test_shutdown_grace_fits_inside_the_termination_period(service: Path) -> None:
    documents = manifests(service)
    pod = documents["Deployment"]["spec"]["template"]["spec"]
    grace = int(documents["ConfigMap"]["data"]["WINDOPS_SHUTDOWN_GRACE_SECONDS"])
    assert pod["terminationGracePeriodSeconds"] > grace


def test_secrets_come_from_a_secret_and_never_a_configmap(service: Path) -> None:
    documents = manifests(service)
    assert not any("SECRET" in key for key in documents["ConfigMap"]["data"])
    env = documents["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"]
    secret = next(item for item in env if item["name"] == "WINDOPS_IDENTITY_SECRET")
    assert "secretKeyRef" in secret["valueFrom"]


def test_service_is_internal_and_headless(service: Path) -> None:
    spec = manifests(service)["Service"]["spec"]
    assert spec["clusterIP"] == "None"
    assert "type" not in spec
    assert {port["name"] for port in spec["ports"]} == {"grpc", "http"}
