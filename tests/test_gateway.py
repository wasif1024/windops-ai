"""Check the gateway manifests and route validation."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from gateway_routes import RouteError, load_plugin_metadata, load_routes, validate_route

ROOT = Path(__file__).resolve().parent.parent
GATEWAY = ROOT / "services" / "api-gateway" / "deploy"
ETCD = ROOT / "infrastructure" / "gateway" / "etcd"
LOCAL_OVERLAY = ROOT / "infrastructure" / "kubernetes" / "overlays" / "local"
GOOD_ROUTE: dict[str, Any] = {
    "uri": "/api/*",
    "plugins": {"opentelemetry": {"sampler": {"name": "always_on"}}},
    "upstream": {
        "nodes": {"windops-web-bff:8080": 1},
        "checks": {"active": {"http_path": "/health/ready"}},
        "timeout": {"connect": 2, "send": 10, "read": 10},
    },
}


def render(path: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def find(documents: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(item for item in documents if item["kind"] == kind)


def gateway_config() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(
        find(render(GATEWAY), "ConfigMap")["data"]["config.yaml"]
    )
    return config


def test_valid_route_passes() -> None:
    validate_route("good.json", GOOD_ROUTE)


@pytest.mark.parametrize(
    "route",
    [
        {"plugins": GOOD_ROUTE["plugins"], "upstream": GOOD_ROUTE["upstream"]},
        {"uri": "/x", "plugins": GOOD_ROUTE["plugins"]},
        {
            "uri": "/x",
            "plugins": GOOD_ROUTE["plugins"],
            "upstream": {"nodes": {}, "checks": GOOD_ROUTE["upstream"]["checks"]},
        },
        {"uri": "/x", "plugins": GOOD_ROUTE["plugins"], "upstream": {"nodes": {"a:1": 1}}},
        {
            "uri": "/x",
            "plugins": GOOD_ROUTE["plugins"],
            "upstream": {"nodes": {"a:1": 1}, "checks": {"active": {}}},
        },
    ],
    ids=["no-uri", "no-upstream", "no-nodes", "no-checks", "no-http-path"],
)
def test_invalid_route_is_rejected(route: dict[str, Any]) -> None:
    with pytest.raises(RouteError):
        validate_route("bad.json", route)


@pytest.mark.parametrize("missing", ["connect", "send", "read"])
def test_route_without_a_timeout_is_rejected(missing: str) -> None:
    upstream = dict(GOOD_ROUTE["upstream"])
    upstream["timeout"] = {key: 5 for key in ("connect", "send", "read") if key != missing}
    with pytest.raises(RouteError):
        validate_route(
            "bad.json", {"uri": "/x", "plugins": GOOD_ROUTE["plugins"], "upstream": upstream}
        )


def test_route_without_tracing_is_rejected() -> None:
    route = {key: value for key, value in GOOD_ROUTE.items() if key != "plugins"}
    with pytest.raises(RouteError):
        validate_route("bad.json", route)


def test_gateway_sends_traces_to_the_collector() -> None:
    metadata = load_plugin_metadata()["opentelemetry"]
    assert metadata["resource"]["service.name"] == "windops-gateway"
    assert "windops-otel-collector" in metadata["collector"]["address"]


def test_gateway_enables_only_the_plugins_it_uses() -> None:
    config = gateway_config()
    assert config["plugins"] == ["opentelemetry", "cors", "limit-count", "openid-connect"]
    assert config["stream_plugins"] == []


def test_route_files_are_loaded_and_validated(tmp_path: Path) -> None:
    (tmp_path / "web-bff.json").write_text(json.dumps(GOOD_ROUTE))
    assert list(load_routes(tmp_path)) == ["web-bff"]
    (tmp_path / "bad.json").write_text(json.dumps({"uri": "/x"}))
    with pytest.raises(RouteError):
        load_routes(tmp_path)


def test_every_route_in_the_repository_is_valid() -> None:
    load_routes()


def test_service_exposes_only_the_proxy_inside_the_cluster() -> None:
    spec = find(render(GATEWAY), "Service")["spec"]
    assert spec.get("type", "ClusterIP") == "ClusterIP"
    # HTTPS only (API-001). Plain HTTP is bound to the pod's loopback address.
    assert [port["targetPort"] for port in spec["ports"]] == ["https"]


def test_admin_api_requires_a_key_and_allows_only_the_pod() -> None:
    admin = gateway_config()["deployment"]["admin"]
    assert admin["admin_key_required"] is True
    assert admin["allow_admin"] == ["127.0.0.0/24"]
    assert admin["admin_key"][0]["key"] == "${{APISIX_ADMIN_KEY}}"


def test_admin_key_comes_from_a_secret() -> None:
    container = find(render(GATEWAY), "Deployment")["spec"]["template"]["spec"]["containers"][0]
    variable = next(item for item in container["env"] if item["name"] == "APISIX_ADMIN_KEY")
    assert "secretKeyRef" in variable["valueFrom"]


def test_control_api_stays_inside_the_pod() -> None:
    assert gateway_config()["apisix"]["control"]["ip"] == "127.0.0.1"


def test_gateway_uses_etcd() -> None:
    deployment = gateway_config()["deployment"]
    assert deployment["role_traditional"]["config_provider"] == "etcd"
    assert "windops-etcd.windops-system" in deployment["etcd"]["host"][0]


@pytest.mark.parametrize(("path", "kind"), [(GATEWAY, "Deployment"), (ETCD, "StatefulSet")])
def test_workload_is_pinned_limited_and_non_root(path: Path, kind: str) -> None:
    pod = find(render(path), kind)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert "@sha256:" in container["image"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(container)
    for section in ("requests", "limits"):
        assert set(container["resources"][section]) == {"cpu", "memory"}


def test_gateway_waits_for_etcd_before_starting() -> None:
    pod = find(render(GATEWAY), "Deployment")["spec"]["template"]["spec"]
    waiter = pod["initContainers"][0]
    assert "windops-etcd" in " ".join(waiter["command"])
    assert waiter["image"] == pod["containers"][0]["image"]
    assert set(waiter["resources"]["limits"]) == {"cpu", "memory"}


def test_etcd_keeps_data_and_stays_internal() -> None:
    documents = render(ETCD)
    statefulset = find(documents, "StatefulSet")
    assert statefulset["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] == "data"
    spec = find(documents, "Service")["spec"]
    # Not headless, because clients keep a stable address through a pod restart.
    assert "clusterIP" not in spec
    assert spec.get("type", "ClusterIP") == "ClusterIP"


def test_local_overlay_includes_the_gateway_with_its_config_unchanged() -> None:
    documents = render(LOCAL_OVERLAY)
    names = {item["metadata"]["name"] for item in documents if item["kind"] == "Deployment"}
    assert "windops-gateway" in names
    config = next(
        item for item in documents if item["metadata"]["name"] == "windops-gateway-config"
    )
    assert set(config["data"]) == {"config.yaml"}


def test_plain_http_stays_inside_the_gateway_pod() -> None:
    """API-001: the browser reaches the gateway over HTTPS only."""
    apisix = gateway_config()["apisix"]
    assert apisix["node_listen"] == [{"ip": "127.0.0.1", "port": 9080}]
    assert apisix["ssl"]["enable"] is True
    assert apisix["ssl"]["listen"] == [{"port": 9443}]
    assert "TLSv1.1" not in apisix["ssl"]["ssl_protocols"]


def test_only_keycloaks_sign_in_pages_are_routed() -> None:
    """Its admin API and the master realm, which lead to the admin console, stay inside."""
    route = load_routes()["keycloak"]
    assert route["uris"] == ["/auth/realms/*", "/auth/resources/*"]
    assert route["vars"] == [["uri", "!", "~~", "^/auth/realms/master(/|$)"]]
    upstream = route["upstream"]
    assert upstream["scheme"] == "https" and upstream["tls"]["verify"] is True
    assert upstream["checks"]["active"]["https_verify_certificate"] is True


def test_the_api_route_checks_tokens_at_the_edge() -> None:
    """SRS 7.2: OIDC validation at the gateway, as well as in Web BFF."""
    oidc = load_routes()["web-bff"]["plugins"]["openid-connect"]
    assert oidc["bearer_only"] is True and oidc["use_jwks"] is True and oidc["ssl_verify"] is True
    assert oidc["claim_validator"]["issuer"]["valid_issuers"] == [
        "https://localhost:9443/auth/realms/windops"
    ]
    assert oidc["claim_validator"]["audience"]["match_with_client_id"] is True
    assert oidc["client_id"] == "windops-api"
    assert "tenant_id" in oidc["claim_schema"]["required"]
    assert "client_secret" not in oidc


def test_cors_names_its_origins_and_never_allows_cookies() -> None:
    """API-005: an explicit allowlist, never a wildcard with credentials."""
    for name in ("web-bff", "web-bff-public"):
        cors = load_routes()[name]["plugins"]["cors"]
        assert "*" not in cors["allow_origins"]
        assert cors["allow_credential"] is False


def test_the_srs_per_address_limits() -> None:
    """SRS 7.5: anonymous login is 5 a minute for each address."""
    routes = load_routes()
    login = routes["keycloak-login"]
    assert login["uri"] == "/auth/realms/windops/login-actions/*"
    assert login["plugins"]["limit-count"]["count"] == 5
    assert login["plugins"]["limit-count"]["key"] == "remote_addr"
    assert login["priority"] > 0


def test_web_bff_is_reached_over_tls_and_its_certificate_checked() -> None:
    for name in ("web-bff", "web-bff-public"):
        upstream = load_routes()[name]["upstream"]
        assert upstream["scheme"] == "https" and upstream["tls"]["verify"] is True
        assert all(node.endswith(":8443") for node in upstream["nodes"])


def test_the_frontend_never_answers_for_keycloak_or_the_api() -> None:
    """An address no route allows, such as Keycloak's admin console, still gets a 404."""
    route = load_routes()["frontend"]
    assert route["uri"] == "/*" and route["priority"] < 0
    assert route["vars"] == [["uri", "!", "~~", "^/(auth|api)(/|$)"]]
    assert route["methods"] == ["GET", "HEAD"]
