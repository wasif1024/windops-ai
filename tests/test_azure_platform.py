"""The services' deployment onto AKS, checked without Azure (scripts/setup/azure_platform.py)."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.setup import azure_deploy, azure_platform
from scripts.setup.azure_platform import (
    DATABASE_WORKLOADS,
    SIGNING_KEY,
    WORKLOADS,
    ca_secret,
    issuer_documents,
    migration_job,
    monitoring_documents,
    needed_secrets,
    postgres_documents,
    service_documents,
    settings_data,
    vault_values,
)

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS_JSON = ROOT / "infrastructure" / "azure" / "bicep" / "generated" / "workloads.json"
PIPELINES = ROOT / "infrastructure" / "azure" / "pipelines"
TAG = "0123456789ab"


def fake_id(index: int, kind: int) -> str:
    return f"{kind:08x}-0000-4000-8000-{index:012x}"


@pytest.fixture(scope="module")
def outputs() -> dict[str, Any]:
    workloads = json.loads(WORKLOADS_JSON.read_text())
    return {
        "keyVaultName": "kv-windops-staging-abc123",
        "registryName": "crwindopsstagingabc123",
        "tenantId": fake_id(0, 9),
        "postgresHost": "psql-windops-staging-abc123.postgres.database.azure.com",
        "postgresAdminClientId": fake_id(0, 7),
        "serviceBusNamespace": "sb-windops-staging-abc123.servicebus.windows.net",
        "gatewayFqdn": "windops-staging-abc123.swedencentral.cloudapp.azure.com",
        "dataResourceGroup": "rg-windops-staging-data",
        "applicationInsightsName": "appi-windops-staging",
        "workloadClientIds": [
            {"name": w["name"], "clientId": fake_id(i, 1), "principalId": fake_id(i, 2)}
            for i, w in enumerate(workloads)
        ],
    }


@pytest.fixture(scope="module")
def services(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    return service_documents(outputs, outputs["registryName"], TAG)


def deployments(documents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["name"]: d for d in documents if d["kind"] == "Deployment"}


def test_it_deploys_the_services_that_need_no_keycloak_redis_or_etcd(services: Any) -> None:
    assert set(deployments(services)) == set(WORKLOADS)
    assert set(DATABASE_WORKLOADS) == {
        "windops-asset", "windops-audit", "windops-authorization", "windops-tenant",
    }  # fmt: skip
    # Each one has its service account for Workload ID, and its SecretProviderClass.
    for kind in ("ServiceAccount", "SecretProviderClass", "Certificate", "ConfigMap"):
        names = {d["metadata"]["name"].removesuffix("-tls").removesuffix("-config")
                 for d in services if d["kind"] == kind}  # fmt: skip
        assert names == set(WORKLOADS), kind


def test_every_placeholder_is_filled_and_images_come_from_the_registry(
    services: Any, outputs: dict[str, Any]
) -> None:
    text = json.dumps(services)
    assert "${AZURE_" not in text
    for name, deployment in deployments(services).items():
        spec = deployment["spec"]["template"]["spec"]
        assert spec["serviceAccountName"] == name
        assert (
            deployment["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"]
            == "true"
        )
        for container in spec["containers"]:
            assert container["image"] == f"{outputs['registryName']}.azurecr.io/{name}:{TAG}"
            names = {item["name"] for item in container.get("env") or []}
            assert not names & {"WINDOPS_DATABASE_PASSWORD", "WINDOPS_EVENTS_PASSWORD"}


def test_key_vault_holds_every_secret_the_services_read_and_no_password(
    services: Any, outputs: dict[str, Any]
) -> None:
    values = vault_values(outputs)
    assert needed_secrets(services) == set(values) | {SIGNING_KEY}
    assert values["windops-audit-database--username"] == "id-windops-audit"
    assert values["windops-audit-database--database"] == "windops_audit"
    assert values["windops-audit-database--host"] == outputs["postgresHost"]
    assert not any("password" in name or "secret" in name for name in values)


def test_the_database_job_runs_as_postgres_entra_administrator(outputs: dict[str, Any]) -> None:
    account, job = postgres_documents(outputs, "registry/windops-asset:tag")
    assert account["metadata"] == {
        "name": "windops-postgres-admin",
        "namespace": "windops-data",
        "annotations": {"azure.workload.identity/client-id": outputs["postgresAdminClientId"]},
    }
    template = job["spec"]["template"]
    assert template["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert template["spec"]["serviceAccountName"] == "windops-postgres-admin"
    command = template["spec"]["containers"][0]["command"]
    assert command[:4] == ["python", "-m", "windops_migrations", "provision-entra"]
    assert command[command.index("--admin") + 1] == "id-windops-postgres-admin"
    roles = [command[i + 1] for i, item in enumerate(command) if item == "--role"]
    principals = {item["name"]: item["principalId"] for item in outputs["workloadClientIds"]}
    assert roles == [f"id-{w}={principals[w]}={w.replace('-', '_')}" for w in DATABASE_WORKLOADS]
    assert "env" not in template["spec"]["containers"][0]  # no password, a token


def test_migrations_sign_in_as_the_service_through_workload_id() -> None:
    path = ROOT / "services" / "audit-service" / "deploy" / "migrate-job.yaml"
    job = migration_job(path, "registry/windops-audit:tag", "windops-audit")
    template = job["spec"]["template"]
    assert template["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert template["spec"]["serviceAccountName"] == "windops-audit"
    container = template["spec"]["containers"][0]
    assert container["image"] == "registry/windops-audit:tag"
    env = {item["name"]: item for item in container["env"]}
    assert "WINDOPS_DATABASE_PASSWORD" not in env
    assert env["WINDOPS_DATABASE_AUTH"] == {"name": "WINDOPS_DATABASE_AUTH", "value": "entra"}
    volumes = {v["name"]: v for v in template["spec"]["volumes"]}
    assert volumes["secrets-store"]["csi"]["volumeAttributes"] == {
        "secretProviderClass": "windops-audit"
    }


def test_cert_manager_issues_from_the_ca_kept_in_key_vault() -> None:
    documents = issuer_documents()
    kinds = [(d["kind"], d["metadata"]["name"]) for d in documents]
    assert ("ClusterIssuer", "windops-internal") in kinds
    assert ("ClusterIssuer", "windops-selfsigned") not in kinds
    assert not any(d["kind"] == "Certificate" and d["spec"].get("isCA") for d in documents)
    assert sum(1 for kind, name in kinds if name == "windops-trust") == 3

    pem = azure_deploy.make_ca("staging")
    secret = ca_secret(pem)
    assert secret["type"] == "kubernetes.io/tls"
    assert secret["metadata"] == {"name": "windops-internal-ca", "namespace": "cert-manager"}
    data = secret["stringData"]
    assert "PRIVATE KEY" in data["tls.key"] and "PRIVATE KEY" not in data["tls.crt"]
    assert data["ca.crt"] == data["tls.crt"]


def test_the_cluster_settings_point_at_azure_and_hold_no_secret(outputs: dict[str, Any]) -> None:
    data = settings_data(outputs, TAG)
    assert data["WINDOPS_ENVIRONMENT"] == "staging"
    assert data["WINDOPS_VERSION"] == TAG
    assert data["WINDOPS_SERVICEBUS_NAMESPACE"] == outputs["serviceBusNamespace"]
    assert data["WINDOPS_PUBLIC_URL"] == f"https://{outputs['gatewayFqdn']}"
    assert data["WINDOPS_DATABASE_AUTH"] == data["WINDOPS_EVENTS_BACKEND"] == "auto"
    assert not any(re.search(r"(PASSWORD|SECRET|KEY)$", name) for name in data)
    assert not any(name.startswith("AZURE_") for name in data)


def test_the_collector_is_filled_in_for_application_insights(outputs: dict[str, Any]) -> None:
    documents = monitoring_documents(outputs)
    assert "${AZURE_" not in json.dumps(documents)  # ${env:...} is the Collector's own
    namespaces = {(d["kind"], d["metadata"]["namespace"]) for d in documents}
    assert ("Deployment", "windops-observability") in namespaces
    assert ("ConfigMap", "kube-system") in namespaces  # managed Prometheus's scrape job


def test_the_pipeline_deploys_the_platform_between_deploy_and_prove() -> None:
    text = (PIPELINES / "azure-pipelines.yml").read_text()
    order = [
        text.index(f"- stage: {name}")
        for name in ("Validate", "Deploy", "Platform", "Prove", "RemoveCompute")
    ]
    assert order == sorted(order)
    assert "azure_deploy platform" in text and "dependsOn: [Deploy, Platform, Prove]" in text
    assert "tests/test_azure_platform.py" in text


def test_postgresql_is_stopped_every_night_unless_a_cluster_uses_it() -> None:
    pipeline = yaml.safe_load((PIPELINES / "azure-postgres-stop.yml").read_text())
    assert pipeline["trigger"] == "none" and pipeline["pr"] == "none"
    assert pipeline["schedules"][0]["always"] is True
    assert "azure_deploy stop-postgres" in json.dumps(pipeline["steps"])


class FakeAz:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers, self.calls = answers, list[str]()

    def __call__(self, *arguments: str, capture: bool = True) -> str:
        command = " ".join(arguments[:5])
        self.calls.append(command)
        return next((v for k, v in self.answers.items() if command.startswith(k)), "")


def test_postgresql_is_left_running_while_the_compute_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAz({"group exists --name rg-windops-staging": "true"})
    monkeypatch.setattr(azure_deploy, "az", fake)
    monkeypatch.setattr(azure_deploy, "az_json", lambda *_: pytest.fail("listed servers"))
    assert azure_deploy.stop_postgres({"environment": "staging", "subscription": "s"}) == 0
    assert not any("stop" in call for call in fake.calls)


def test_postgresql_is_stopped_when_no_cluster_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAz({"group exists --name rg-windops-staging-data": "true"})
    monkeypatch.setattr(azure_deploy, "az", fake)
    monkeypatch.setattr(azure_deploy, "az_json", lambda *_: [{"n": "psql", "s": "Ready"}])
    azure_deploy.stop_postgres({"environment": "staging", "subscription": "s"})
    assert "postgres flexible-server stop --resource-group rg-windops-staging-data" in fake.calls


def test_the_images_are_built_for_the_nodes_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(azure_platform, "az", lambda *_: "")
    monkeypatch.setattr("subprocess.run", lambda command, **_: commands.append(command))
    azure_platform.push_images("crwindops", TAG, ["windops-audit"])
    build, push = commands
    assert build[:2] == ["docker", "build"] and "linux/amd64" in build
    assert build[build.index("--file") + 1].endswith("services/audit-service/Dockerfile")
    assert push == ["docker", "push", "--quiet", f"crwindops.azurecr.io/windops-audit:{TAG}"]


def test_each_pipeline_agent_lets_itself_through_the_firewalls(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any]
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(azure_deploy, "public_ip", lambda: "203.0.113.7")

    def az(*arguments: str) -> str:
        calls.append(arguments)
        return ""

    monkeypatch.setattr(azure_deploy, "az", az)
    monkeypatch.setattr(azure_deploy, "az_json", lambda *arguments: ["198.51.100.0/24"])
    monkeypatch.setattr("time.sleep", lambda _: None)
    cluster = {**outputs, "computeResourceGroup": "rg-c", "clusterName": "aks"}
    config = {"subscription": "s", "configured_ranges": ["198.51.100.0/24"]}
    azure_deploy.allow_this_machine(config, cluster)
    commands = {" ".join(call[:5]) for call in calls}
    assert "aks update --subscription s --resource-group" in commands
    update = next(call for call in calls if call[:2] == ("aks", "update"))
    # The cluster's ranges are replaced, so the existing ones stay.
    assert update[-1] == "198.51.100.0/24,203.0.113.7/32"
    assert any(call[:3] == ("keyvault", "network-rule", "add") for call in calls)
    assert any(call[:5] == ("servicebus", "namespace", "network-rule-set", "ip-rule", "add")
               for call in calls)  # fmt: skip

    calls.clear()
    monkeypatch.setattr(azure_deploy, "public_ip", lambda: "198.51.100.9")
    azure_deploy.allow_this_machine(config, cluster)
    assert calls == []  # an operator's configured range is already allowed


def test_a_stopped_postgresql_is_started_before_it_is_deployed_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAz({"group exists --name rg-windops-staging-data": "true"})
    monkeypatch.setattr(azure_deploy, "az", fake)
    monkeypatch.setattr(azure_deploy, "az_json", lambda *_: [{"n": "psql", "s": "Stopped"}])
    azure_deploy.start_postgres({"environment": "staging", "subscription": "s"})
    assert "postgres flexible-server start --resource-group rg-windops-staging-data" in fake.calls
    source = (ROOT / "scripts" / "setup" / "azure_deploy.py").read_text()
    up = source[source.index("def up(") : source.index("def down(")]
    assert up.index("start_postgres(config)") < up.index("deployCompute=False")
