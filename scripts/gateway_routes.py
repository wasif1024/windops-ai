#!/usr/bin/env python3
"""Apply the gateway's routes, stored as JSON files, through the APISIX Admin API.

The Admin API listens inside the pod only, so this reaches it through a port-forward.
"""

import contextlib
import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

REPOSITORY = Path(__file__).resolve().parent.parent
ROUTES_DIRECTORY = REPOSITORY / "infrastructure" / "gateway" / "routes"
PLUGIN_METADATA_DIRECTORY = REPOSITORY / "infrastructure" / "gateway" / "plugin-metadata"
ADMIN_KEY_FILE = REPOSITORY / "infrastructure" / "minikube" / "secrets.local.gateway.env"
CONTEXT = "windops"
NAMESPACE = "windops-system"
DEPLOYMENT = "deployment/windops-gateway"
ADMIN_PORT = 9180


class RouteError(Exception):
    """A route file is invalid, or could not be applied."""


def validate_route(name: str, route: dict[str, Any]) -> None:
    """Require tracing, health checks and timeouts on every route."""
    if "opentelemetry" not in route.get("plugins", {}):
        raise RouteError(f"{name}: a route needs the opentelemetry plugin, so it can be traced")
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


def load_routes(directory: Path = ROUTES_DIRECTORY) -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        route = json.loads(path.read_text())
        validate_route(path.name, route)
        routes[path.stem] = route
    return routes


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def port_forward(target: str, namespace: str, remote_port: int) -> Iterator[int]:
    """Forward a local port to a workload, and return that port once it accepts connections."""
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
