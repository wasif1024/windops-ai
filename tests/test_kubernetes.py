"""Render every overlay and check it against SRS section 11."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from new_service import add_service_to_kustomization, create_service

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "infrastructure" / "kubernetes" / "base"
OVERLAYS = ROOT / "infrastructure" / "kubernetes" / "overlays"
OVERLAY_NAMES = ["ci", "local", "production-example"]
SRS_NAMESPACES = {
    "windops-system",
    "windops-application",
    "windops-mcp",
    "windops-data",
    "windops-observability",
}


def render(path: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


@pytest.mark.parametrize("overlay", OVERLAY_NAMES)
def test_overlay_creates_the_srs_namespaces(overlay: str) -> None:
    documents = render(OVERLAYS / overlay)
    names = {item["metadata"]["name"] for item in documents if item["kind"] == "Namespace"}
    assert names == SRS_NAMESPACES


@pytest.mark.parametrize("overlay", OVERLAY_NAMES)
def test_overlay_exposes_nothing_publicly(overlay: str) -> None:
    """DEP-007: APISIX, added in M1.8, is the only allowed exception."""
    for document in render(OVERLAYS / overlay):
        assert document["kind"] != "Ingress"
        if document["kind"] == "Service":
            assert document["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_our_workload_namespaces_enforce_pod_security() -> None:
    for document in render(BASE / "namespaces"):
        labels = document["metadata"]["labels"]
        if document["metadata"]["name"] in {"windops-application", "windops-mcp"}:
            assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
        assert labels["pod-security.kubernetes.io/warn"] == "restricted"


def overlay_with_service(tmp_path: Path, overlay: str) -> list[dict[str, Any]]:
    """Render an overlay as if a service existed, without changing the repository."""
    kubernetes = tmp_path / "infrastructure" / "kubernetes"
    kubernetes.parent.mkdir(parents=True)
    shutil.copytree(ROOT / "infrastructure" / "kubernetes", kubernetes)
    create_service("telemetry-service", tmp_path)
    file = kubernetes / "base" / "services" / "kustomization.yaml"
    file.write_text(add_service_to_kustomization("telemetry-service", file.read_text()))
    return render(kubernetes / "overlays" / overlay)


@pytest.mark.parametrize(
    ("overlay", "replicas", "environment"),
    [("local", 1, "local"), ("ci", 1, "ci"), ("production-example", 2, "production-example")],
)
def test_overlay_sets_replicas_and_environment(
    overlay: str, replicas: int, environment: str, tmp_path: Path
) -> None:
    documents = overlay_with_service(tmp_path, overlay)
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    config = next(item for item in documents if item["kind"] == "ConfigMap")
    assert deployment["spec"]["replicas"] == replicas
    assert config["data"]["WINDOPS_ENVIRONMENT"] == environment
    assert deployment["metadata"]["namespace"] == "windops-application"


def test_local_overlay_meets_k8s_007(tmp_path: Path) -> None:
    documents = overlay_with_service(tmp_path, "local")
    assert not [item for item in documents if item["kind"] == "HorizontalPodAutoscaler"]
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    limits = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert "memory" in limits


def test_service_is_listed_once_in_the_kustomization() -> None:
    text = (BASE / "services" / "kustomization.yaml").read_text()
    once = add_service_to_kustomization("telemetry-service", text)
    twice = add_service_to_kustomization("telemetry-service", once)
    assert once == twice
    assert yaml.safe_load(once)["resources"] == ["../../../../services/telemetry-service/deploy"]
    second = add_service_to_kustomization("alarm-service", once)
    assert yaml.safe_load(second)["resources"] == [
        "../../../../services/alarm-service/deploy",
        "../../../../services/telemetry-service/deploy",
    ]
