#!/usr/bin/env python3
"""Prove logs, traces, metrics and dashboards work end to end on the real local cluster.

Run `make cluster` first. It sends one signed gRPC call to a throwaway service, then finds that
call in Tempo, Loki and Prometheus. The service is removed afterwards, and the stack stays.
"""

import contextlib
import json
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import grpc
import httpx
from gateway_routes import port_forward
from verify_cluster import IGNORED, kubectl, minikube_docker_env, run
from windops_grpc import VerifiedIdentity, sign_identity

REPOSITORY = Path(__file__).resolve().parent.parent
OBSERVABILITY = "windops-observability"
APPLICATION = "windops-application"
DIRECTORY = "observe-probe-service"
WORKLOAD = "windops-observe-probe"
PACKAGE = "windops_observe_probe"
IMAGE = f"{WORKLOAD}:local"
DASHBOARDS = REPOSITORY / "infrastructure" / "monitoring" / "grafana" / "dashboards"
GRAFANA_PASSWORD_FILE = REPOSITORY / "infrastructure" / "minikube" / "secrets.local.grafana.env"
IDENTITY_KEY_FILE = REPOSITORY / "infrastructure" / "minikube" / "secrets.local.env"
WORKLOADS = (
    "deployment/windops-otel-collector",
    "statefulset/windops-tempo",
    "statefulset/windops-prometheus",
    "deployment/windops-kube-state-metrics",
    "deployment/windops-alertmanager",
    "deployment/windops-grafana",
    "statefulset/windops-loki",
    "daemonset/windops-fluent-bit",
)
# Adds a test method, so a call passes through the interceptor as a real one would.
PROBE_REGISTER = '''def register(server: grpc.aio.Server) -> None:
    """Add a test method to the throwaway service."""

    async def echo(request: bytes, context: object) -> bytes:
        return request

    handler = grpc.unary_unary_rpc_method_handler(echo)
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("windops.probe.v1.Probe", {"Echo": handler}),)
    )
'''


def wait_until(description: str, check: Any, *, attempts: int = 60, delay: float = 5) -> Any:
    last: object = None
    for _ in range(attempts):
        try:
            result = check()
            if result:
                print(description, flush=True)
                return result
            last = result
        except (httpx.HTTPError, KeyError, ValueError, grpc.RpcError) as error:
            last = error
        time.sleep(delay)
    raise RuntimeError(f"Timed out waiting: {description}. Last result: {last}")


def deploy_probe(root: Path) -> dict[str, str]:
    run("make", "service", f"NAME={DIRECTORY}", cwd=root)
    main = root / "services" / DIRECTORY / "src" / PACKAGE / "main.py"
    text = main.read_text()
    start = text.index("def register(")
    end = text.index("def main(")
    main.write_text(text[:start] + PROBE_REGISTER + "\n\n" + text[end:])
    env = minikube_docker_env()
    run(
        "docker", "build", "-q", "-f", f"services/{DIRECTORY}/Dockerfile", "-t", IMAGE, ".",
        cwd=root, env=env,
    )  # fmt: skip
    kubectl("apply", "--kustomize", f"{root}/services/{DIRECTORY}/deploy")
    kubectl(
        "rollout", "status", f"deployment/{WORKLOAD}", f"--namespace={APPLICATION}",
        "--timeout=240s",
    )  # fmt: skip
    return env


def send_call(trace_id: str, request_id: str) -> None:
    key = IDENTITY_KEY_FILE.read_text().split("=", 1)[1].strip()
    token = sign_identity(VerifiedIdentity("svc-observe-check", "tenant-observe"), key)
    deployment = f"deployment/{WORKLOAD}"
    with port_forward(deployment, APPLICATION, 50051) as port:
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        method: Any = channel.unary_unary("/windops.probe.v1.Probe/Echo")
        result = method(
            b"ping",
            timeout=10,
            metadata=(
                ("x-windops-identity", token),
                ("x-windops-request-id", request_id),
                ("traceparent", f"00-{trace_id}-b7ad6b7169203331-01"),
            ),
        )
        channel.close()
    if result != b"ping":
        raise RuntimeError("The call returned the wrong response")
    print(f"Sent one signed gRPC call, with trace {trace_id}")


def check_microservice_panels_fill_with_traffic() -> None:
    """Rates need at least two scrapes, so send calls until those panels have data."""
    key = IDENTITY_KEY_FILE.read_text().split("=", 1)[1].strip()
    token = sign_identity(VerifiedIdentity("svc-observe-check", "tenant-observe"), key)
    required = [
        (title, expression)
        for title, expression in dashboard_queries()
        if title.startswith("Microservice Health") and "Circuit breaker" not in title
    ]
    with (
        port_forward(f"deployment/{WORKLOAD}", APPLICATION, 50051) as service_port,
        port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as prometheus_port,
    ):
        channel = grpc.insecure_channel(f"127.0.0.1:{service_port}")
        method: Any = channel.unary_unary("/windops.probe.v1.Probe/Echo")
        missing = [title for title, _ in required]
        for _ in range(48):
            method(b"ping", timeout=10, metadata=(("x-windops-identity", token),))
            with contextlib.suppress(grpc.RpcError):
                method(b"ping", timeout=10)  # Rejected, to fill the failures panel.
            missing = [
                title
                for title, expression in required
                if not prometheus_query(prometheus_port, expression)
            ]
            if not missing:
                break
            time.sleep(5)
        channel.close()
    if missing:
        raise RuntimeError(f"These panels never showed data: {missing}")
    print(f"With traffic and rejected calls, all {len(required)} request panels show data")


def check_tempo(trace_id: str) -> None:
    with port_forward("service/windops-tempo", OBSERVABILITY, 3200) as port:

        def found() -> bool:
            response = httpx.get(f"http://127.0.0.1:{port}/api/traces/{trace_id}", timeout=10)
            return response.status_code == 200 and "windops.probe.v1.Probe/Echo" in response.text

        wait_until("Tempo has the call's trace", found)


def check_loki(trace_id: str, request_id: str) -> None:
    query = f'{{namespace="{APPLICATION}"}} |= "{request_id}"'

    def lines(port: int) -> list[str]:
        response = httpx.get(
            f"http://127.0.0.1:{port}/loki/api/v1/query_range",
            params={"query": query, "limit": 20, "since": "15m"},
            timeout=10,
        )
        streams = response.json()["data"]["result"]
        return [line for stream in streams for _, line in stream["values"]]

    with port_forward("service/windops-loki", OBSERVABILITY, 3100) as port:
        found = wait_until("Loki has the call's log line", lambda: lines(port))
    joined = "\n".join(found)
    if trace_id not in joined:
        raise RuntimeError(f"The log line has no trace ID:\n{joined}")
    if "tenant-observe" not in joined:
        raise RuntimeError(f"The log line has no tenant:\n{joined}")
    print("The log line has the same trace ID and the verified tenant")


def prometheus_query(port: int, expression: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"http://127.0.0.1:{port}/api/v1/query", params={"query": expression}, timeout=15
    )
    body = response.json()
    if body["status"] != "success":
        raise RuntimeError(f"Invalid query {expression}: {body}")
    result: list[dict[str, Any]] = body["data"]["result"]
    return result


def dashboard_queries() -> list[tuple[str, str]]:
    queries = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        board = json.loads(path.read_text())
        for panel in board["panels"]:
            for target in panel["targets"]:
                queries.append((f"{board['title']}: {panel['title']}", target["expr"]))
    return queries


def check_pod_labels(port: int) -> None:
    """Restart and readiness data must name the real pod, not the pod that reports it."""
    expected = {
        "kube_pod_status_ready": ("windops-system", "windops-etcd-0"),
        "kube_pod_container_status_restarts_total": ("windops-system", "windops-etcd-0"),
        "container_memory_working_set_bytes": ("windops-system", "windops-etcd-0"),
    }
    for metric, (namespace, pod) in expected.items():
        selector = f'{metric}{{namespace="{namespace}",pod="{pod}"}}'
        if not prometheus_query(port, selector):
            raise RuntimeError(f"Prometheus reports the wrong pod labels for {metric}")
    # After a config change, series with old labels take up to five minutes to age out.
    own_series = (
        'kube_pod_status_ready{namespace="windops-observability",'
        'pod=~"windops-kube-state-metrics.*",condition="true"}'
    )
    wait_until(
        "Restart, readiness and memory data name the real pod and namespace",
        lambda: len(prometheus_query(port, own_series)) == 1,
        attempts=72,
    )


def check_prometheus() -> None:
    with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as port:
        call_metric = (
            f'windops_rpc_server_duration_seconds_count{{workload="{WORKLOAD}",rpc_status="OK"}}'
        )
        wait_until("Prometheus has the call's metric", lambda: prometheus_query(port, call_metric))
        for label in ("cadvisor", "pods"):
            wait_until(
                f"Prometheus is scraping {label}",
                lambda label=label: prometheus_query(port, f'up{{job="{label}"}} == 1'),
            )
        empty = []
        for title, expression in dashboard_queries():
            if not prometheus_query(port, expression):
                empty.append(title)
        print(f"All {len(dashboard_queries())} dashboard queries are valid")
        must_have_data = {
            "Platform Health: CPU by pod",
            "Platform Health: Memory by pod",
            "Platform Health: Pod restarts in the last hour",
            "Platform Health: Pods not ready",
        }
        if must_have_data & set(empty):
            raise RuntimeError(f"These panels have no data: {must_have_data & set(empty)}")
        print(f"Panels that need an event or traffic first: {empty or 'none'}")
        check_pod_labels(port)
        rules = httpx.get(f"http://127.0.0.1:{port}/api/v1/rules", timeout=10).json()
        names = {rule["name"] for group in rules["data"]["groups"] for rule in group["rules"]}
        if not {"WindopsTargetDown", "WindopsCircuitBreakerOpen"} <= names:
            raise RuntimeError(f"Alert rules are missing: {names}")
        alertmanagers = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/alertmanagers", timeout=10
        ).json()
        if not alertmanagers["data"]["activeAlertmanagers"]:
            raise RuntimeError("Prometheus has no Alertmanager")
        print("Alert rules are loaded, and Prometheus is connected to Alertmanager")


def check_grafana() -> None:
    password = GRAFANA_PASSWORD_FILE.read_text().split("=", 1)[1].strip()
    with port_forward("service/windops-grafana", OBSERVABILITY, 3000) as port:
        base = f"http://127.0.0.1:{port}"
        if httpx.get(f"{base}/api/search", timeout=10).status_code != 401:
            raise RuntimeError("Grafana answered without a login")
        print("Grafana requires a login")
        auth = ("admin", password)
        titles = {
            item["title"] for item in httpx.get(f"{base}/api/search", auth=auth, timeout=10).json()
        }
        if not {"Platform Health", "Microservice Health", "Events"} <= titles:
            raise RuntimeError(f"Dashboards are missing: {titles}")
        print("Grafana has the Platform Health, Microservice Health and Events dashboards")
        for uid in ("prometheus", "loki", "tempo"):
            health = httpx.get(
                f"{base}/api/datasources/uid/{uid}/health", auth=auth, timeout=30
            ).json()
            if health.get("status") != "OK":
                raise RuntimeError(f"Grafana data source {uid} is unhealthy: {health}")
        print("Grafana reaches Prometheus, Loki and Tempo")


def report_memory() -> None:
    print("Measured:")
    print(
        kubectl(
            "top", "pod", f"--namespace={OBSERVABILITY}", "--no-headers", check=False
        ).stdout.rstrip()
    )
    usage = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", CLUSTER_CONTAINER],
        capture_output=True, text=True, check=False,
    ).stdout.strip()  # fmt: skip
    print(f"Whole cluster: {usage}")


CLUSTER_CONTAINER = "windops"


def main() -> int:
    run("make", "secrets", cwd=REPOSITORY)
    run("make", "infra", cwd=REPOSITORY)
    for workload in WORKLOADS:
        kubectl("rollout", "status", workload, f"--namespace={OBSERVABILITY}", "--timeout=480s")
    print("The observability stack is running")
    trace_id = secrets.token_hex(16)
    request_id = f"observe-check-{secrets.token_hex(6)}"
    with tempfile.TemporaryDirectory(prefix="windops-observe-") as temporary:
        root = Path(temporary) / "repository"
        shutil.copytree(REPOSITORY, root, ignore=IGNORED)
        env = deploy_probe(root)
        try:
            send_call(trace_id, request_id)
            check_tempo(trace_id)
            check_loki(trace_id, request_id)
            check_prometheus()
            check_microservice_panels_fill_with_traffic()
            check_grafana()
            report_memory()
        finally:
            kubectl(
                "delete", "--kustomize", f"{root}/services/{DIRECTORY}/deploy", "--ignore-not-found"
            )
            kubectl(
                "wait", "--for=delete", "pod", f"--namespace={APPLICATION}",
                f"--selector=app.kubernetes.io/name={WORKLOAD}", "--timeout=60s",
                check=False,
            )  # fmt: skip
            run("docker", "image", "rm", IMAGE, env=env, check=False)
    print("Logs, traces, metrics and dashboards work on the cluster.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
