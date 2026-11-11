"""The azure overlay: Workload ID and Key Vault for each workload, and no DB or NATS secret."""

import subprocess
from pathlib import Path
from typing import Any

import yaml

from scripts.generate.azure_overlay import OVERLAY, REMOVED, client_id_setting, generate

ROOT = Path(__file__).resolve().parent.parent


def rendered(overlay: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / "infrastructure/kubernetes/overlays" / overlay)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [d for d in yaml.safe_load_all(out) if d]


def references(deployment: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (item["name"], ref["name"], ref["key"])
        for c in deployment["spec"]["template"]["spec"]["containers"]
        for item in c.get("env") or []
        if (ref := (item.get("valueFrom") or {}).get("secretKeyRef"))
    ]


def test_the_overlay_is_up_to_date() -> None:
    """When this fails, run: uv run python -m scripts.generate.azure_overlay"""
    for path, text in generate().items():
        assert (OVERLAY / path).read_text() == text, path


def test_every_workload_has_its_own_identity_and_no_stored_database_or_nats_secret() -> None:
    documents = rendered("azure")
    accounts = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in documents
                if d["kind"] == "ServiceAccount"}  # fmt: skip
    providers = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in documents
                 if d["kind"] == "SecretProviderClass"}  # fmt: skip
    deployments = [d for d in documents if d["kind"] == "Deployment"]
    covered = 0
    for deployment in deployments:
        name, namespace = deployment["metadata"]["name"], deployment["metadata"]["namespace"]
        spec = deployment["spec"]["template"]["spec"]
        refs = references(deployment)
        if (namespace, name) not in accounts:
            assert not refs, f"{name} reads Secrets but has no workload identity"
            continue
        covered += 1
        assert spec["serviceAccountName"] == name
        assert deployment["spec"]["template"]["metadata"]["labels"][
            "azure.workload.identity/use"] == "true"  # fmt: skip
        annotation = accounts[(namespace, name)]["metadata"]["annotations"]
        assert (
            annotation["azure.workload.identity/client-id"] == "${" + client_id_setting(name) + "}"
        )
        assert not [var for var, _, _ in refs if var in REMOVED], name
        synced = {
            (obj["secretName"], item["key"])
            for obj in providers[(namespace, name)]["spec"]["secretObjects"]
            for item in obj["data"]
        }
        assert {(secret, key) for _, secret, key in refs} <= synced, name
        assert any(v.get("csi", {}).get("volumeAttributes", {}).get("secretProviderClass") == name
                   for v in spec["volumes"])  # fmt: skip
    assert covered >= 9


def test_the_database_and_events_need_no_secret_on_azure() -> None:
    telemetry = next(d for d in rendered("azure") if d["kind"] == "Deployment"
                     and d["metadata"]["name"] == "windops-telemetry")  # fmt: skip
    names = {var for var, _, _ in references(telemetry)}
    assert "WINDOPS_DATABASE_USERNAME" in names  # its Entra principal, from Key Vault
    assert names.isdisjoint(REMOVED)


def test_the_local_overlay_keeps_passwords_and_no_workload_identity() -> None:
    documents = rendered("local")
    assert not [d for d in documents if d["kind"] in ("ServiceAccount", "SecretProviderClass")]
    asset = next(d for d in documents if d["kind"] == "Deployment"
                 and d["metadata"]["name"] == "windops-asset")  # fmt: skip
    assert "WINDOPS_DATABASE_PASSWORD" in {var for var, _, _ in references(asset)}
