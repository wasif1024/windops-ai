"""Check the observability manifests and dashboards against SRS section 10."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "infrastructure" / "kubernetes" / "base" / "observability"
DASHBOARDS = ROOT / "infrastructure" / "monitoring" / "grafana" / "dashboards"
# SRS section 11.2, plus the additions recorded in ADR 0002.
EXPECTED_WORKLOADS = {
    "windops-otel-collector",
    "windops-prometheus",
    "windops-alertmanager",
    "windops-grafana",
    "windops-loki",
    "windops-fluent-bit",
    "windops-tempo",
    "windops-kube-state-metrics",
}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}


@pytest.fixture(scope="module")
def documents() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(STACK)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def workloads(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in documents if item["kind"] in WORKLOAD_KINDS]


def config_data(documents: list[dict[str, Any]], name: str, key: str) -> Any:
    config = next(item for item in documents if item["metadata"]["name"] == name)
    return yaml.safe_load(config["data"][key])


def test_every_expected_workload_exists(documents: list[dict[str, Any]]) -> None:
    assert {item["metadata"]["name"] for item in workloads(documents)} == EXPECTED_WORKLOADS


def test_everything_namespaced_is_in_the_observability_namespace(
    documents: list[dict[str, Any]],
) -> None:
    for item in documents:
        if item["kind"] not in {"ClusterRole", "ClusterRoleBinding"}:
            assert item["metadata"]["namespace"] == "windops-observability", item["metadata"]


def test_nothing_is_exposed_outside_the_cluster(documents: list[dict[str, Any]]) -> None:
    for item in documents:
        assert item["kind"] != "Ingress"
        if item["kind"] == "Service":
            assert item["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_every_workload_is_pinned_probed_and_limited(documents: list[dict[str, Any]]) -> None:
    for item in workloads(documents):
        for container in item["spec"]["template"]["spec"]["containers"]:
            name = item["metadata"]["name"]
            assert "@sha256:" in container["image"], name
            assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(container), name
            for section in ("requests", "limits"):
                assert set(container["resources"][section]) == {"cpu", "memory"}, name
            assert container["securityContext"]["allowPrivilegeEscalation"] is False, name
            assert container["securityContext"]["capabilities"]["drop"] == ["ALL"], name


def test_only_fluent_bit_runs_without_a_non_root_user(documents: list[dict[str, Any]]) -> None:
    for item in workloads(documents):
        security = item["spec"]["template"]["spec"].get("securityContext", {})
        if item["metadata"]["name"] == "windops-fluent-bit":
            continue
        assert security["runAsNonRoot"] is True, item["metadata"]["name"]


def test_fluent_bit_has_only_read_access_to_host_logs(documents: list[dict[str, Any]]) -> None:
    fluent = next(item for item in workloads(documents) if item["kind"] == "DaemonSet")
    mounts = fluent["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert all(mount["readOnly"] is True for mount in mounts)


def test_fluent_bit_skips_the_stacks_own_logs(documents: list[dict[str, Any]]) -> None:
    config = next(
        item for item in documents if item["metadata"]["name"] == "windops-fluent-bit-config"
    )
    text = config["data"]["fluent-bit.conf"]
    assert "*_windops-observability_*" in text


def test_grafana_requires_login_and_gets_its_password_from_a_secret(
    documents: list[dict[str, Any]],
) -> None:
    grafana = next(i for i in workloads(documents) if i["metadata"]["name"] == "windops-grafana")
    env = {
        item["name"]: item for item in grafana["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["GF_AUTH_ANONYMOUS_ENABLED"]["value"] == "false"
    assert env["GF_USERS_ALLOW_SIGN_UP"]["value"] == "false"
    assert "secretKeyRef" in env["GF_SECURITY_ADMIN_PASSWORD"]["valueFrom"]


def test_prometheus_roles_can_only_read(documents: list[dict[str, Any]]) -> None:
    for item in documents:
        if item["kind"] == "ClusterRole":
            for rule in item["rules"]:
                assert set(rule["verbs"]) <= {"get", "list", "watch"}, item["metadata"]["name"]


def test_retention_is_set_for_each_store(documents: list[dict[str, Any]]) -> None:
    """OBS-004. docs/observability.md lists the same values."""
    loki = config_data(documents, "windops-loki-config", "loki.yaml")
    assert loki["limits_config"]["retention_period"] == "168h"
    assert loki["compactor"]["retention_enabled"] is True
    tempo = config_data(documents, "windops-tempo-config", "tempo.yaml")
    assert tempo["compactor"]["compaction"]["block_retention"] == "24h"
    prometheus = next(
        item for item in workloads(documents) if item["metadata"]["name"] == "windops-prometheus"
    )
    arguments = prometheus["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--storage.tsdb.retention.time=7d" in arguments
    documentation = (ROOT / "docs" / "observability.md").read_text()
    for value in ("7 days", "24 hours"):
        assert value in documentation


def test_collector_sends_traces_to_tempo(documents: list[dict[str, Any]]) -> None:
    config = config_data(documents, "windops-otel-collector-config", "config.yaml")
    assert config["service"]["pipelines"]["traces"]["exporters"] == ["otlp_grpc/tempo"]
    assert "windops-tempo" in config["exporters"]["otlp_grpc/tempo"]["endpoint"]


@pytest.mark.parametrize("path", sorted(DASHBOARDS.glob("*.json")), ids=lambda path: path.stem)
def test_dashboards_are_valid_with_stable_ids(path: Path) -> None:
    board = json.loads(path.read_text())
    assert board["uid"].startswith("windops-")
    ids = [panel["id"] for panel in board["panels"]]
    assert len(ids) == len(set(ids))
    for panel in board["panels"]:
        assert panel["targets"][0]["expr"]
        assert panel["datasource"]["uid"] == "prometheus"


def panel_titles(name: str) -> str:
    board = json.loads((DASHBOARDS / name).read_text())
    return " ".join(panel["title"].lower() for panel in board["panels"])


def test_platform_health_has_the_srs_indicators_available_so_far() -> None:
    titles = panel_titles("platform-health.json")
    for indicator in ("cpu", "memory", "restarts", "not ready"):
        assert indicator in titles


def test_microservice_health_has_the_srs_indicators() -> None:
    titles = panel_titles("microservice-health.json")
    for indicator in ("request rate", "latency", "failures", "circuit breaker"):
        assert indicator in titles


def test_service_template_opts_in_to_scraping() -> None:
    template = ROOT / "scripts" / "service_template" / "deploy" / "deployment.yaml.tmpl"
    text = template.read_text()
    assert 'prometheus.io/scrape: "true"' in text
    assert 'prometheus.io/port: "8080"' in text


def test_kube_state_metrics_keeps_the_labels_of_the_pods_it_reports_on() -> None:
    config = yaml.safe_load(
        (ROOT / "infrastructure" / "monitoring" / "prometheus" / "prometheus.yml").read_text()
    )
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert jobs["kube-state-metrics"]["honor_labels"] is True
    # Services must not be able to set their own namespace or pod labels.
    assert "honor_labels" not in jobs["pods"]
    drop = [rule for rule in jobs["pods"]["relabel_configs"] if rule.get("action") == "drop"]
    assert drop[0]["regex"] == "windops-kube-state-metrics"


def test_platform_panels_use_pod_level_figures_that_exist_on_every_runtime() -> None:
    text = (DASHBOARDS / "platform-health.json").read_text()
    assert "container!=" not in text
    assert 'container=\\"\\"' in text


def test_a_prometheus_config_change_restarts_prometheus(documents: list[dict[str, Any]]) -> None:
    prometheus = next(
        item for item in workloads(documents) if item["metadata"]["name"] == "windops-prometheus"
    )
    volume = prometheus["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"]
    assert volume.startswith("windops-prometheus-config-")
