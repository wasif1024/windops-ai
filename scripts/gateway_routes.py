#!/usr/bin/env python3
"""Apply the gateway's routes, stored as JSON files, through the APISIX Admin API.

The Admin API listens inside the pod only, so this reaches it through a port-forward. A route
names a setting as ${WINDOPS_NAME}, which is filled in from scripts/settings.py, so an
environment's addresses change in .env, not in the routes.
"""

import base64
import contextlib
import json
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from settings import KUBE_CONTEXT, load

REPOSITORY = Path(__file__).resolve().parent.parent
ROUTES_DIRECTORY = REPOSITORY / "infrastructure" / "gateway" / "routes"
PLUGIN_METADATA_DIRECTORY = REPOSITORY / "infrastructure" / "gateway" / "plugin-metadata"
ADMIN_KEY_FILE = REPOSITORY / "infrastructure" / "minikube" / "secrets.local.gateway.env"
CONTEXT = KUBE_CONTEXT
NAMESPACE = "windops-system"
DEPLOYMENT = "deployment/windops-gateway"
ADMIN_PORT = 9180


class RouteError(Exception):
    """A route file is invalid, or could not be applied."""


def validate_route(name: str, route: dict[str, Any]) -> None:
    """Require tracing, counting, health checks and timeouts on every route."""
    if "opentelemetry" not in route.get("plugins", {}):
        raise RouteError(f"{name}: a route needs the opentelemetry plugin, so it can be traced")
    if "prometheus" not in route.get("plugins", {}):
        raise RouteError(f"{name}: a route needs the prometheus plugin, so its answers are counted")
    if "uri" not in route and "uris" not in route:
        raise RouteError(f"{name}: a route needs a uri")
    upstream = route.get("upstream")
    if not isinstance(upstream, dict) or not upstream.get("nodes"):
        raise RouteError(f"{name}: a route needs an upstream with nodes")
    active = upstream.get("checks", {}).get("active")
    if not active or not active.get("http_path"):
        raise RouteError(f"{name}: an upstream needs active health checks with an http_path")
    # Without these, APISIX waits up to 60 seconds for a backend that is down.
    timeout = upstream.get("timeout", {})
    if not {"connect", "send", "read"} <= set(timeout):
        raise RouteError(f"{name}: an upstream needs connect, send and read timeouts")


_PLACEHOLDER = re.compile(r"\$\{(WINDOPS_[A-Z_]+)\}")


def fill_in(name: str, text: str, settings: dict[str, str] | None = None) -> str:
    """Replace each ${WINDOPS_NAME} with its setting, keeping the JSON valid."""
    values = load() if settings is None else settings

    def setting(match: re.Match[str]) -> str:
        value = values.get(match.group(1), "")
        if not value:
            raise RouteError(f"{name}: {match.group(1)} is not set")
        if '"' in value or "\\" in value:
            raise RouteError(f"{name}: {match.group(1)} cannot hold a quote or a backslash")
        return value

    return _PLACEHOLDER.sub(setting, text)


def load_routes(
    directory: Path = ROUTES_DIRECTORY, settings: dict[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        route = json.loads(fill_in(path.name, path.read_text(), settings))
        validate_route(path.name, route)
        routes[path.stem] = route
    return routes


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def port_forward(
    target: str, namespace: str, remote_port: int, *, local_port: int | None = None
) -> Iterator[int]:
    """Forward a local port to a workload, and return that port once it accepts connections.

    Pass `local_port` only when the address matters, such as Keycloak's fixed public one.
    """
    if local_port is None:
        local_port = _free_port()
    process = subprocess.Popen(
        [
            "kubectl", "--context", CONTEXT, "port-forward", f"--namespace={namespace}",
            target, f"{local_port}:{remote_port}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RouteError(f"The port-forward to {target} stopped")
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", local_port)) == 0:
                    yield local_port
                    return
            time.sleep(0.1)
        raise RouteError(f"The port-forward to {target} did not start")
    finally:
        process.terminate()
        process.wait(timeout=10)


def admin_key(path: Path = ADMIN_KEY_FILE) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("admin-key="):
            return line.removeprefix("admin-key=")
    raise RouteError(f"{path.name} has no admin-key. Run: make secrets")


def apply_route(base_url: str, key: str, route_id: str, route: dict[str, Any]) -> None:
    response = httpx.put(
        f"{base_url}/apisix/admin/routes/{route_id}",
        headers={"X-API-KEY": key},
        json=route,
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise RouteError(f"Route {route_id} failed: {response.status_code} {response.text}")


CERTIFICATE_RESOURCE = "windops-gateway-tls"
# The names a browser or check may use, so APISIX picks this certificate for them.
SERVER_NAMES = [
    "localhost",
    "windops-gateway",
    "windops-gateway.windops-system.svc.cluster.local",
]


def gateway_certificate() -> dict[str, str]:
    """Read the gateway's certificate and key from cert-manager's Secret."""
    result = subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "get", "secret", CERTIFICATE_RESOURCE,
            f"--namespace={NAMESPACE}", "--output=json",
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RouteError(f"No certificate for the gateway yet: {result.stderr.strip()}")
    data = json.loads(result.stdout)["data"]
    return {
        "cert": base64.b64decode(data["tls.crt"]).decode(),
        "key": base64.b64decode(data["tls.key"]).decode(),
    }


def apply_certificate(base_url: str, key: str) -> None:
    body = {**gateway_certificate(), "snis": SERVER_NAMES}
    response = httpx.put(
        f"{base_url}/apisix/admin/ssls/windops-gateway",
        headers={"X-API-KEY": key},
        json=body,
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise RouteError(f"The certificate failed: {response.status_code}")


def load_plugin_metadata(directory: Path = PLUGIN_METADATA_DIRECTORY) -> dict[str, dict[str, Any]]:
    """Load each plugin's gateway-wide settings. The file name is the plugin's name."""
    return {path.stem: json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))}


def apply_plugin_metadata(base_url: str, key: str, plugin: str, metadata: dict[str, Any]) -> None:
    response = httpx.put(
        f"{base_url}/apisix/admin/plugin_metadata/{plugin}",
        headers={"X-API-KEY": key},
        json=metadata,
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise RouteError(f"Plugin {plugin} failed: {response.status_code} {response.text}")


def main() -> int:
    try:
        routes = load_routes()
        if not routes:
            print("No routes to apply yet.")
            return 0
        key = admin_key()
        with port_forward(DEPLOYMENT, NAMESPACE, ADMIN_PORT) as port:
            apply_certificate(f"http://127.0.0.1:{port}", key)
            print("Applied the gateway's HTTPS certificate")
            for plugin, metadata in load_plugin_metadata().items():
                apply_plugin_metadata(f"http://127.0.0.1:{port}", key, plugin, metadata)
                print(f"Applied plugin settings for {plugin}")
            for route_id, route in routes.items():
                apply_route(f"http://127.0.0.1:{port}", key, route_id, route)
                print(f"Applied route {route_id}")
    except (RouteError, OSError, httpx.HTTPError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
