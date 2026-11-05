#!/usr/bin/env python3
"""Prove the gateway works on the real local cluster, using a throwaway service.

Run `make cluster` first. The service runs from a temporary copy of the repository, and the
gateway and service are removed afterwards. etcd stays, because `make infra` owns it.
"""

import shutil
import tempfile
import time
from pathlib import Path

import httpx
from gateway_routes import apply_certificate, apply_route, port_forward, validate_route
from tls_client import trust_cluster_ca
from verify_cluster import IGNORED, kubectl, minikube_docker_env, run

REPOSITORY = Path(__file__).resolve().parent.parent
SYSTEM = "windops-system"
APPLICATION = "windops-application"
DIRECTORY = "gateway-probe-service"
WORKLOAD = "windops-gateway-probe"
IMAGE = f"{WORKLOAD}:local"
HOST = f"{WORKLOAD}.{APPLICATION}.svc.cluster.local"
ROUTE_ID = "gateway-check"
ROUTE = {
    "uri": "/health/live",
    "plugins": {"opentelemetry": {"sampler": {"name": "always_on"}}},
    "upstream": {
        "type": "roundrobin",
        "retries": 0,
        "timeout": {"connect": 2, "send": 5, "read": 5},
        # The second node accepts nothing. Only health checks can keep traffic away from it.
        "nodes": {f"{HOST}:8080": 1, f"{HOST}:1": 1},
        "checks": {
            "active": {
                "type": "http",
                "http_path": "/health/ready",
                "timeout": 1,
                "healthy": {"interval": 1, "successes": 1},
                "unhealthy": {"interval": 1, "http_failures": 1, "tcp_failures": 1, "timeouts": 1},
            }
        },
    },
}


def wait_rollout(kind_name: str, namespace: str) -> None:
    kubectl("rollout", "status", kind_name, f"--namespace={namespace}", "--timeout=240s")


def get_statuses(url: str, count: int) -> list[int]:
    return [httpx.get(url, timeout=5).status_code for _ in range(count)]


def wait_for_status(url: str, expected: int, *, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            if httpx.get(url, timeout=3).status_code == expected:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"{url} never returned {expected}")


def remove_route(admin_url: str, key: str) -> None:
    """Delete the test route, retrying while the gateway reconnects to etcd."""
    started = time.monotonic()
    last: object = None
    for _ in range(6):
        try:
            response = httpx.delete(
                f"{admin_url}/apisix/admin/routes/{ROUTE_ID}",
                headers={"X-API-KEY": key},
                timeout=40,
            )
            if response.status_code in (200, 404):
                print(f"The Admin API removed the test route in {time.monotonic() - started:.1f}s")
                return
            last = response.status_code
        except httpx.HTTPError as error:
            last = error
        time.sleep(2)
    raise RuntimeError(f"Could not remove the test route: {last}")


def check_service_exposes_only_the_proxy() -> None:
    service = kubectl(
        "get", "service", "windops-gateway", f"--namespace={SYSTEM}",
        "--output=jsonpath={.spec.type} {.spec.ports[*].targetPort}",
    ).stdout.strip()  # fmt: skip
    if service != "ClusterIP https":
        raise RuntimeError(f"The Service exposes more than the proxy: {service}")
    print("The Service exposes only the HTTPS proxy port, inside the cluster")


def check_admin_rejects_wrong_key(admin_url: str, key: str) -> None:
    url = f"{admin_url}/apisix/admin/routes"
    if httpx.get(url, timeout=5).status_code != 401:
        raise RuntimeError("The Admin API answered without a key")
    if httpx.get(url, headers={"X-API-KEY": "wrong"}, timeout=5).status_code != 401:
        raise RuntimeError("The Admin API answered with a wrong key")
    if httpx.get(url, headers={"X-API-KEY": key}, timeout=5).status_code != 200:
        raise RuntimeError("The Admin API refused the real key")
    print("The Admin API requires its key")


def check_admin_is_private() -> None:
    """A pod elsewhere must not reach the Admin API, even at the gateway pod's own address."""
    address = kubectl(
        "get", "pods", f"--namespace={SYSTEM}", "--selector=app.kubernetes.io/name=windops-gateway",
        "--output=jsonpath={.items[0].status.podIP}",
    ).stdout.strip()  # fmt: skip
    name = f"{WORKLOAD}-client"
    result = kubectl(
        "run", name, "--namespace=default", "--image=busybox:1.37.0", "--restart=Never",
        "--attach", "--quiet", "--pod-running-timeout=120s", "--command", "--",
        "sh", "-c", f"wget -T 4 -S -O- http://{address}:9180/apisix/admin/routes 2>&1 || true",
        check=False,
    )  # fmt: skip
    kubectl("delete", "pod", name, "--namespace=default", "--ignore-not-found", "--now")
    if "403" not in result.stdout:
        raise RuntimeError(f"The Admin API did not refuse another pod:\n{result.stdout}")
    print("The Admin API refused another pod, because it allows only the pod itself")


def check_unhealthy_node_is_skipped(proxy: str, control: str) -> None:
    url = f"{proxy}/health/live"
    health = ""
    for _ in range(40):
        response = httpx.get(f"{control}/v1/healthcheck/routes/{ROUTE_ID}", timeout=5)
        health = response.text
        if response.status_code == 200 and "unhealthy" in health and '"healthy"' in health:
            break
        # Upstream health checks start with the first request through the route.
        httpx.get(url, timeout=5)
        time.sleep(1)
    else:
        raise RuntimeError(f"The gateway never marked the bad node unhealthy:\n{health}")
    statuses = get_statuses(url, 40)
    if set(statuses) != {200}:
        raise RuntimeError(f"Requests reached the unhealthy node: {sorted(set(statuses))}")
    print("Health checks marked the bad node unhealthy, and 40 of 40 requests avoided it")


def check_backend_outage(proxy: str) -> None:
    url = f"{proxy}/health/live"
    deployment = f"deployment/{WORKLOAD}"
    kubectl("scale", deployment, f"--namespace={APPLICATION}", "--replicas=0")
    kubectl(
        "wait", "--for=delete", "pod", f"--namespace={APPLICATION}",
        f"--selector=app.kubernetes.io/name={WORKLOAD}", "--timeout=60s",
    )  # fmt: skip
    for _ in range(40):
        if httpx.get(url, timeout=8).status_code >= 500:
            break
        time.sleep(1)
    else:
        raise RuntimeError("The gateway kept returning success during an outage")
    print("With the backend down, the gateway returned an error")
    kubectl("scale", deployment, f"--namespace={APPLICATION}", "--replicas=1")
    wait_rollout(deployment, APPLICATION)
    wait_for_status(url, 200)
    print("The gateway recovered when the backend returned")


def restart_and_check_route(
    kind_name: str, namespace: str, selector: str, port_target: str
) -> None:
    kubectl("delete", "pod", f"--namespace={namespace}", f"--selector={selector}", "--wait=true")
    wait_rollout(kind_name, namespace)
    wait_rollout("deployment/windops-gateway", SYSTEM)
    with port_forward(port_target, SYSTEM, 9443) as proxy_port:
        wait_for_status(f"https://127.0.0.1:{proxy_port}/health/live", 200)
    print(f"The route survived a restart of {kind_name}")


def check_gateway(root: Path, key: str) -> None:
    run("make", "service", f"NAME={DIRECTORY}", cwd=root)
    env = minikube_docker_env()
    run(
        "docker", "build", "-q", "-f", f"services/{DIRECTORY}/Dockerfile", "-t", IMAGE, ".",
        cwd=root, env=env,
    )  # fmt: skip
    kubectl("apply", "--kustomize", f"{root}/services/{DIRECTORY}/deploy")
    gateway = "deployment/windops-gateway"
    try:
        wait_rollout(f"deployment/{WORKLOAD}", APPLICATION)
        wait_rollout(gateway, SYSTEM)
        check_service_exposes_only_the_proxy()
        check_admin_is_private()
        validate_route(ROUTE_ID, ROUTE)
        with (
            port_forward(gateway, SYSTEM, 9180) as admin_port,
            port_forward(gateway, SYSTEM, 9443) as proxy_port,
            port_forward(gateway, SYSTEM, 9090) as control_port,
        ):
            admin = f"http://127.0.0.1:{admin_port}"
            proxy = f"https://127.0.0.1:{proxy_port}"
            check_admin_rejects_wrong_key(admin, key)
            apply_certificate(admin, key)
            # Routes persist in etcd, so an interrupted run can leave this one behind.
            remove_route(admin, key)
            if httpx.get(f"{proxy}/health/live", timeout=5).status_code != 404:
                raise RuntimeError("The gateway answered a path with no route")
            print("With no route, the gateway returned 404")
            apply_route(admin, key, ROUTE_ID, ROUTE)
            wait_for_status(f"{proxy}/health/live", 200)
            print("With a route, the gateway reached the service")
            check_unhealthy_node_is_skipped(proxy, f"http://127.0.0.1:{control_port}")
            check_backend_outage(proxy)
        restart_and_check_route(gateway, SYSTEM, "app.kubernetes.io/name=windops-gateway", gateway)
        restart_and_check_route(
            "statefulset/windops-etcd", SYSTEM, "app.kubernetes.io/name=windops-etcd", gateway
        )
        print(
            "Measured:\n"
            + kubectl(
                "top", "pod", f"--namespace={SYSTEM}", "--no-headers", check=False
            ).stdout.strip()
        )
    finally:
        with port_forward(gateway, SYSTEM, 9180) as admin_port:
            remove_route(f"http://127.0.0.1:{admin_port}", key)
        kubectl(
            "delete", "--kustomize", f"{root}/services/{DIRECTORY}/deploy", "--ignore-not-found"
        )
        kubectl(
            "wait", "--for=delete", "pod", f"--namespace={APPLICATION}",
            f"--selector=app.kubernetes.io/name={WORKLOAD}", "--timeout=60s",
            check=False,
        )  # fmt: skip
        run("docker", "image", "rm", IMAGE, env=env, check=False)
        kubectl(
            "delete", "--kustomize", f"{root}/services/api-gateway/deploy", "--ignore-not-found"
        )


def main() -> int:
    trust_cluster_ca()
    run("make", "secrets", cwd=REPOSITORY)
    run("make", "infra", cwd=REPOSITORY)
    wait_rollout("statefulset/windops-etcd", SYSTEM)
    run("make", "gateway", cwd=REPOSITORY)
    key_file = REPOSITORY / "infrastructure" / "minikube" / "secrets.local.gateway.env"
    key = key_file.read_text().split("=", 1)[1].strip()
    with tempfile.TemporaryDirectory(prefix="windops-gateway-") as temporary:
        root = Path(temporary) / "repository"
        shutil.copytree(REPOSITORY, root, ignore=IGNORED)
        check_gateway(root, key)
    print("The gateway works on the cluster.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
