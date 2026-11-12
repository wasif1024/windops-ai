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
        if panel["type"] == "text":
            continue
        assert panel["targets"][0]["expr"]
        assert panel["datasource"]["uid"] == "prometheus"


def panel_titles(name: str) -> str:
    board = json.loads((DASHBOARDS / name).read_text())
    return " ".join(panel["title"].lower() for panel in board["panels"])


def test_platform_health_has_the_srs_indicators_available_so_far() -> None:
    titles = panel_titles("platform-health.json")
    for indicator in ("cpu", "memory", "restarts", "not ready"):
        assert indicator in titles


def test_pod_health_table_comes_first_and_skips_finished_jobs() -> None:
    """A finished migration Job's pod is never ready, which must not look like a problem."""
    board = json.loads((DASHBOARDS / "platform-health.json").read_text())
    table = board["panels"][0]
    assert (table["title"], table["type"]) == ("Pod health", "table")
    assert (table["gridPos"]["x"], table["gridPos"]["y"]) == (0, 0)
    assert 'phase="Running"' in table["targets"][0]["expr"]
    not_ready = next(panel for panel in board["panels"] if panel["title"] == "Pods not ready")
    assert 'phase="Running"' in not_ready["targets"][0]["expr"]


def test_pod_health_panels_do_not_overlap() -> None:
    board = json.loads((DASHBOARDS / "platform-health.json").read_text())
    cells: set[tuple[int, int]] = set()
    for panel in board["panels"]:
        grid = panel["gridPos"]
        area = {
            (x, y)
            for x in range(grid["x"], grid["x"] + grid["w"])
            for y in range(grid["y"], grid["y"] + grid["h"])
        }
        assert not cells & area, panel["title"]
        cells |= area


def test_platform_health_shows_every_data_store() -> None:
    """SRS 10.3."""
    titles = panel_titles("platform-health.json")
    for store in ("database", "redis", "qdrant", "jetstream"):
        assert store in titles


def test_events_dashboard_has_the_srs_indicators() -> None:
    """SRS 10.3."""
    titles = panel_titles("events.json")
    for indicator in ("consumer lag", "outbox backlog", "dead-letter"):
        assert indicator in titles


def test_security_dashboard_has_the_srs_indicators() -> None:
    """SRS 10.3: failed logins, MFA failures, access denials, rate limits and tool calls."""
    titles = panel_titles("security.json")
    for indicator in (
        "failed sign-ins",
        "mfa failures",
        "access denials",
        "rate-limited",
    ):
        assert indicator in titles
    assert "suspicious tool calls" in titles
    exprs = " ".join(
        target["expr"]
        for panel in json.loads((DASHBOARDS / "security.json").read_text())["panels"]
        for target in panel.get("targets", [])
    )
    for metric in (
        "keycloak_user_events_total",
        "keycloak_credentials_password_hashing_validations_total",
        "windops_audit_records_total",
        "apisix_http_status",
        "windops_web_bff_rate_limited_total",
    ):
        assert metric in exprs


def test_hour_and_day_totals_count_a_counter_that_is_new_in_the_window() -> None:
    """increase() ignores a series' first sample, so after a restart the first events vanish.

    The Phase 4A gate found this: Keycloak restarted, its wrong-password counter appeared at 2,
    and the panel showed 0. Each total uses a series' full value when it is new in the window.
    """
    import re

    window_total = re.compile(r"increase\(([a-z_]+\{[^{}]*\})\[(1h|24h)\]\)")
    for path in sorted(DASHBOARDS.glob("*.json")):
        for panel in json.loads(path.read_text())["panels"]:
            for target in panel.get("targets", []):
                expr = target["expr"]
                for selector, window in window_total.findall(expr):
                    guarded = f"[{window}]) and {selector} offset {window} or {selector})"
                    assert guarded in expr, f"{path.name}: {panel['title']} misses new series"


def test_security_dashboard_panels_do_not_overlap() -> None:
    board = json.loads((DASHBOARDS / "security.json").read_text())
    cells: set[tuple[int, int]] = set()
    for panel in board["panels"]:
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24
        mine = {
            (x, y)
            for x in range(grid["x"], grid["x"] + grid["w"])
            for y in range(grid["y"], grid["y"] + grid["h"])
        }
        assert not cells & mine, panel["title"]
        cells |= mine


@pytest.mark.parametrize(
    "alert",
    [
        "WindopsManyFailedSignIns",
        "WindopsManyAccessDenials",
        "WindopsSustainedRateLimiting",
        "WindopsBreakGlassStarted",
        "WindopsAdministratorRoleGranted",
    ],
)
def test_security_events_have_their_alerts(alert: str) -> None:
    """SRS 10.3: suspicious authorization failures and security-relevant administrator events."""
    rule = alert_rules()[alert]
    assert rule["labels"]["category"] == "security"
    assert rule["labels"]["severity"] in ("info", "warning")


def alert_rules() -> dict[str, dict[str, Any]]:
    rules = yaml.safe_load(
        (ROOT / "infrastructure" / "monitoring" / "prometheus" / "rules.yml").read_text()
    )
    return {rule["alert"]: rule for group in rules["groups"] for rule in group["rules"]}


@pytest.mark.parametrize(
    "alert",
    [
        "WindopsDeadLetterMessages",
        "WindopsOutboxBacklog",
        "WindopsOutboxRowsSetAside",
        "WindopsConsumerLag",
        "WindopsDataStoreDown",
    ],
)
def test_events_have_their_alerts(alert: str) -> None:
    """EVT-003 and SRS 7.7."""
    rule = alert_rules()[alert]
    assert rule["labels"]["severity"] in {"warning", "critical"}
    assert rule["annotations"]["summary"]


def test_tempo_writes_small_blocks() -> None:
    """Its default buffers exceeded its memory limit, while holding under 10 MiB of traces."""
    documents = yaml.safe_load_all(
        (ROOT / "infrastructure" / "monitoring" / "tempo" / "tempo.yaml").read_text()
    )
    config = next(item for item in documents if item["kind"] == "ConfigMap")
    tempo = yaml.safe_load(config["data"]["tempo.yaml"])
    assert tempo["ingester"]["max_block_duration"] == "5m"
    assert tempo["storage"]["trace"]["block"]["parquet_row_group_size_bytes"] <= 32 * 1024 * 1024


def test_repeated_restarts_raise_an_alert() -> None:
    """A crash loop went unnoticed through most of Phase 2, because nothing flagged it."""
    assert (
        "kube_pod_container_status_restarts_total" in alert_rules()["WindopsPodRestarting"]["expr"]
    )


def test_a_dead_letter_alerts_immediately() -> None:
    """EVT-003: one message means something failed repeatedly, or was malformed."""
    assert "for" not in alert_rules()["WindopsDeadLetterMessages"]


def test_microservice_health_has_the_srs_indicators() -> None:
    titles = panel_titles("microservice-health.json")
    for indicator in ("request rate", "latency", "failures", "circuit breaker"):
        assert indicator in titles


def test_service_template_opts_in_to_scraping() -> None:
    template = ROOT / "scripts" / "dev" / "service_template" / "deploy" / "deployment.yaml.tmpl"
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
