"""Azure Monitor's configuration says the same as the local stack's (ADR 0006)."""

import json
from pathlib import Path
from typing import Any

import yaml

from scripts.generate.azure_monitoring import (
    AZURE_OUT,
    CLUSTER_OUT,
    CONNECTION_SECRET,
    DATASOURCE,
    MONITORING,
    SERVICE_BUS_ALERTS,
    SEVERITY,
    generate,
    iso_duration,
)


def documents(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def test_the_azure_files_are_up_to_date() -> None:
    """When this fails, run: uv run python -m scripts.generate.azure_monitoring"""
    for path, text in generate().items():
        assert path.read_text() == text, path


def test_managed_prometheus_scrapes_with_our_own_pods_job() -> None:
    local = yaml.safe_load((MONITORING / "prometheus" / "prometheus.yml").read_text())
    pods = next(job for job in local["scrape_configs"] if job["job_name"] == "pods")
    (config,) = documents(CLUSTER_OUT / "ama-metrics-prometheus-config.yaml")
    assert (config["metadata"]["name"], config["metadata"]["namespace"]) == (
        "ama-metrics-prometheus-config", "kube-system",
    )  # fmt: skip
    assert yaml.safe_load(config["data"]["prometheus-config"])["scrape_configs"] == [pods]


def test_every_alert_is_kept_on_azure_or_covered_by_service_bus() -> None:
    local = yaml.safe_load((MONITORING / "prometheus" / "rules.yml").read_text())
    azure = json.loads((AZURE_OUT / "prometheus-rule-groups.json").read_text())
    converted = {rule["alert"]: rule for group in azure["groups"] for rule in group["rules"]}
    for group in local["groups"]:
        for rule in group["rules"]:
            if rule["alert"] in azure["coveredByServiceBusAlerts"]:
                assert "jetstream_" in rule["expr"]
                continue
            mine = converted[rule["alert"]]
            assert mine["expression"] == rule["expr"]
            assert mine["for"] == (iso_duration(rule["for"]) if "for" in rule else "PT0M")
            assert mine["severity"] == SEVERITY[rule.get("labels", {}).get("severity", "warning")]
    assert not [r for r in converted.values() if "jetstream_" in r["expression"]]
    metrics = {alert["criterion"]["metricName"] for alert in SERVICE_BUS_ALERTS}
    assert "DeadletteredMessages" in metrics


def test_durations_are_written_as_azure_writes_them() -> None:
    assert [iso_duration(v) for v in ("30s", "5m", "1h", "1d")] == ["PT30S", "PT5M", "PT1H", "P1D"]


def test_the_collector_sends_traces_to_application_insights_only() -> None:
    found = {d["kind"]: d for d in documents(CLUSTER_OUT / "collector.yaml")}
    config = yaml.safe_load(found["ConfigMap"]["data"]["config.yaml"])
    assert list(config["exporters"]) == ["azuremonitor"]
    # The connection string comes from Key Vault, through a Secret, never the config.
    assert config["exporters"]["azuremonitor"]["connection_string"].startswith("${env:")
    assert all(p["exporters"] == ["azuremonitor"] for p in config["service"]["pipelines"].values())
    container = found["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert "@sha256:" in container["image"] and "contrib" in container["image"]
    env = {e["name"]: e for e in container["env"]}
    assert env["APPLICATIONINSIGHTS_CONNECTION_STRING"]["valueFrom"]["secretKeyRef"]["name"] == (
        CONNECTION_SECRET
    )
    local = next(d for d in documents(MONITORING / "otel-collector" / "collector.yaml")
                 if d["kind"] == "Deployment")  # fmt: skip
    local_container = local["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"] == local_container["securityContext"]
    assert container["resources"] == local_container["resources"]


def test_every_dashboard_panel_uses_the_azure_monitor_workspace() -> None:
    for path in sorted((AZURE_OUT / "grafana").glob("*.json")):
        text = path.read_text()
        assert '"uid": "prometheus"' not in text, path.name
        local = json.loads((MONITORING / "grafana" / "dashboards" / path.name).read_text())
        assert len(json.loads(text)["panels"]) == len(local["panels"])
        assert DATASOURCE in text
