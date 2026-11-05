"""The certificate authority, and the rule that every real service uses mutual TLS (GRP-005)."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SECURITY = ROOT / "infrastructure" / "security"
# Our own services. The API Gateway is APISIX, and M3.7 secures it.
SERVICES = sorted(path.parent for path in (ROOT / "services").glob("*/pyproject.toml"))
MOUNT = "/var/run/windops/tls"


def rendered(directory: Path) -> list[dict[str, Any]]:
    output = subprocess.run(
        ["kubectl", "kustomize", str(directory)], capture_output=True, text=True, check=True
    ).stdout
    return [item for item in yaml.safe_load_all(output) if item]


def named(items: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(i for i in items if i["kind"] == kind and i["metadata"]["name"] == name)


def hours(value: str) -> int:
    return int(value.removesuffix("h"))


def test_there_are_services_to_check() -> None:
    assert len(SERVICES) >= 3


def test_every_cert_manager_image_is_pinned_and_memory_is_limited() -> None:
    deployments = [i for i in rendered(SECURITY / "cert-manager") if i["kind"] == "Deployment"]
    assert len(deployments) == 3
    for deployment in deployments:
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert "@sha256:" in container["image"]
        assert container["resources"]["limits"]["memory"]


def test_services_are_issued_by_a_ca_and_not_by_the_self_signed_issuer() -> None:
    items = rendered(SECURITY / "pki")
    authority = named(items, "Certificate", "windops-internal-ca")["spec"]
    assert authority["isCA"] is True
    assert authority["issuerRef"]["name"] == "windops-selfsigned"
    issuer = named(items, "ClusterIssuer", "windops-internal")["spec"]
    assert issuer["ca"]["secretName"] == authority["secretName"]


def test_the_ca_keeps_its_key_when_it_renews() -> None:
    """A new key would split services between two CAs, and calls between them would fail."""
    authority = named(rendered(SECURITY / "pki"), "Certificate", "windops-internal-ca")["spec"]
    assert authority["privateKey"]["rotationPolicy"] == "Never"


def test_the_ca_renews_with_time_for_every_service_to_get_the_new_copy() -> None:
    """Services renew every 23 days, so all hold the renewed CA before the old one expires."""
    authority = named(rendered(SECURITY / "pki"), "Certificate", "windops-internal-ca")["spec"]
    certificate = yaml.safe_load(
        (ROOT / "services/sample-responder-service/deploy/certificate.yaml").read_text()
    )["spec"]
    renewal_cycle = hours(certificate["duration"]) - hours(certificate["renewBefore"])
    assert hours(authority["renewBefore"]) > renewal_cycle


@pytest.mark.parametrize("service", SERVICES, ids=lambda path: path.name)
def test_every_service_has_a_certificate_and_uses_it(service: Path) -> None:
    items = rendered(service / "deploy")
    deployment = next(i for i in items if i["kind"] == "Deployment")
    name = deployment["metadata"]["name"]
    certificate = named(items, "Certificate", f"{name}-tls")["spec"]
    namespace = deployment["metadata"]["namespace"]
    assert certificate["commonName"] == name
    assert f"{name}.{namespace}.svc.cluster.local" in certificate["dnsNames"]
    assert certificate["issuerRef"]["name"] == "windops-internal"
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["WINDOPS_TLS_DIR"] == MOUNT
    assert "WINDOPS_PLAINTEXT_GRPC" not in env
    assert "grpc" not in container["startupProbe"]


def test_no_deployed_manifest_turns_off_mutual_tls() -> None:
    for path in [
        *ROOT.glob("services/*/deploy/*.yaml"),
        *ROOT.glob("infrastructure/kubernetes/**/*.yaml"),
    ]:
        assert "WINDOPS_PLAINTEXT_GRPC" not in path.read_text(), path
