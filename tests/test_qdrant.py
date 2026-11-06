"""Check the Qdrant manifests against SRS sections 8 and 11."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.setup.local_secrets import LOCAL_SECRETS

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "infrastructure" / "kubernetes" / "base" / "data"


def documents() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(STACK)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


@pytest.fixture(scope="module")
def workload() -> dict[str, Any]:
    return next(
        item
        for item in documents()
        if item["kind"] == "StatefulSet" and item["metadata"]["name"] == "windops-qdrant"
    )


def container(workload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = workload["spec"]["template"]["spec"]["containers"][0]
    return result


def test_qdrant_is_in_the_data_namespace_and_not_exposed() -> None:
    """DEP-007."""
    service = next(
        item
        for item in documents()
        if item["kind"] == "Service" and item["metadata"]["name"] == "windops-qdrant"
    )
    assert service["metadata"]["namespace"] == "windops-data"
    assert service["spec"]["type"] == "ClusterIP"


def test_qdrant_keeps_data_on_a_persistent_volume(workload: dict[str, Any]) -> None:
    """SRS 8.1 and DEP-006."""
    claim = workload["spec"]["volumeClaimTemplates"][0]["metadata"]["name"]
    assert {"name": claim, "mountPath": "/qdrant/storage"} in container(workload)["volumeMounts"]


def test_snapshots_are_kept_on_the_persistent_volume(workload: dict[str, Any]) -> None:
    env = {item["name"]: item for item in container(workload)["env"]}
    assert env["QDRANT__STORAGE__SNAPSHOTS_PATH"]["value"].startswith("/qdrant/storage/")


def test_qdrant_needs_an_api_key(workload: dict[str, Any]) -> None:
    env = {item["name"]: item for item in container(workload)["env"]}
    key = env["QDRANT__SERVICE__API_KEY"]["valueFrom"]["secretKeyRef"]
    assert key == {"name": "windops-qdrant-api-key", "key": "api-key"}


def test_qdrant_sends_no_usage_data_outside_the_cluster(workload: dict[str, Any]) -> None:
    """EGR-001."""
    env = {item["name"]: item for item in container(workload)["env"]}
    assert env["QDRANT__TELEMETRY_DISABLED"]["value"] == "true"


def test_qdrant_is_pinned_probed_and_limited(workload: dict[str, Any]) -> None:
    item = container(workload)
    assert "@sha256:" in item["image"]
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(item)
    assert "memory" in item["resources"]["limits"]


def test_qdrant_meets_the_restricted_pod_security_profile(workload: dict[str, Any]) -> None:
    pod = workload["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    context = pod["containers"][0]["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"]["drop"] == ["ALL"]


def test_api_key_reaches_only_qdrant_and_its_client() -> None:
    secret = next(item for item in LOCAL_SECRETS if item.name == "windops-qdrant-api-key")
    assert set(secret.namespaces) == {"windops-data", "windops-application"}
