#!/usr/bin/env python3
"""Prove the manifests work on the real local cluster, using a throwaway service.

Run `make cluster`, `make infra` and `make secrets` first. The service runs from a temporary
copy of the repository, so the repository does not change, and it is removed afterwards.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
CONTEXT = "windops"
DIRECTORY = "cluster-probe-service"
WORKLOAD = "windops-cluster-probe"
NAMESPACE = "windops-application"
IMAGE = f"{WORKLOAD}:local"
CLIENT_IMAGE = "busybox:1.37.0"
IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "assets"
)
DENY_INGRESS = f"""
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {WORKLOAD}-deny-check
  namespace: {NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {WORKLOAD}
  policyTypes: [Ingress]
"""
PRIVILEGED_POD = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {WORKLOAD}-privileged-check
  namespace: {NAMESPACE}
spec:
  containers:
    - name: check
      image: {CLIENT_IMAGE}
      securityContext:
        privileged: true
"""


def run(
    *command: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{command[0]} failed:\n{result.stdout}\n{result.stderr}")
    return result


def kubectl(
    *arguments: str, stdin: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run("kubectl", "--context", CONTEXT, *arguments, stdin=stdin, check=check)


def minikube_docker_env() -> dict[str, str]:
    output = run("minikube", "-p", CONTEXT, "docker-env", "--shell", "bash").stdout
    env = dict(os.environ)
    for line in output.splitlines():
        if line.startswith("export "):
            key, _, value = line.removeprefix("export ").partition("=")
            env[key] = value.strip('"')
    return env


def client_request(url: str) -> bool:
    """Return whether a pod elsewhere in the cluster can reach a URL."""
    name = f"{WORKLOAD}-client"
    kubectl("delete", "pod", name, "--namespace=default", "--ignore-not-found", "--now")
    result = kubectl(
        "run", name, "--namespace=default", f"--image={CLIENT_IMAGE}", "--restart=Never",
        "--attach", "--quiet", "--pod-running-timeout=120s",
        "--command", "--", "wget", "-T", "4", "-qO-", url,
        check=False,
    )  # fmt: skip
    kubectl("delete", "pod", name, "--namespace=default", "--ignore-not-found", "--now")
    return result.returncode == 0 and "alive" in result.stdout


def check_overlays(root: Path) -> None:
    for overlay in ("local", "ci", "production-example"):
        kubectl(
            "apply",
            "--dry-run=server",
            "--kustomize",
            f"{root}/infrastructure/kubernetes/overlays/{overlay}",
        )


def deployed_pod() -> dict[str, object]:
    output = kubectl(
        "get", "pods", f"--namespace={NAMESPACE}",
        f"--selector=app.kubernetes.io/name={WORKLOAD}", "--output=json",
    ).stdout  # fmt: skip
    pods = json.loads(output)["items"]
    if len(pods) != 1:
        raise RuntimeError(f"Expected one pod, found {len(pods)}")
    pod: dict[str, object] = pods[0]
    return pod


def check_pod_security() -> None:
    result = kubectl("apply", "--dry-run=server", "--filename=-", stdin=PRIVILEGED_POD, check=False)
    if result.returncode == 0 or "violates PodSecurity" not in result.stderr:
        raise RuntimeError(f"A privileged pod was not refused:\n{result.stderr}")
    print("A privileged pod was refused, as Pod Security requires")


def check_network_policy() -> None:
    url = f"http://{WORKLOAD}.{NAMESPACE}.svc.cluster.local:8080/health/live"
    if not client_request(url):
        raise RuntimeError("Another pod could not reach the service")
    kubectl("apply", "--filename=-", stdin=DENY_INGRESS)
    try:
        time.sleep(3)
        if client_request(url):
            raise RuntimeError("Calico did not enforce the NetworkPolicy")
    finally:
        kubectl("delete", "networkpolicy", f"{WORKLOAD}-deny-check", f"--namespace={NAMESPACE}")
    print("Calico enforced a NetworkPolicy, and removing it restored access")


def measure_memory() -> str:
    for _ in range(36):
        result = kubectl(
            "top", "pod", f"--namespace={NAMESPACE}",
            f"--selector=app.kubernetes.io/name={WORKLOAD}", "--no-headers", check=False,
        )  # fmt: skip
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        time.sleep(5)
    raise RuntimeError("metrics-server reported no memory figure")


def check_deployment(root: Path) -> None:
    run("make", "service", f"NAME={DIRECTORY}", cwd=root)
    env = minikube_docker_env()
    run(
        "docker", "build", "-q", "-f", f"services/{DIRECTORY}/Dockerfile", "-t", IMAGE, ".",
        cwd=root, env=env,
    )  # fmt: skip
    overlay = f"{root}/infrastructure/kubernetes/overlays/local"
    check_overlays(root)
    rendered = kubectl("kustomize", overlay).stdout
    for expected in (f"name: {WORKLOAD}", "WINDOPS_ENVIRONMENT: local"):
        if expected not in rendered:
            raise RuntimeError(f"The local overlay did not produce {expected!r}")
    kubectl("apply", "--kustomize", overlay)
    try:
        kubectl(
            "rollout", "status", f"deployment/{WORKLOAD}", f"--namespace={NAMESPACE}",
            "--timeout=180s",
        )  # fmt: skip
        pod = deployed_pod()
        status = pod["status"]
        assert isinstance(status, dict)  # noqa: S101
        container = status["containerStatuses"][0]
        if not container["ready"] or container["restartCount"] != 0:
            raise RuntimeError(f"The pod is unhealthy: {container}")
        print("The pod is ready, with passing probes and no restarts")
        check_pod_security()
        check_network_policy()
        print(f"Measured: {measure_memory()}")

        metadata = pod["metadata"]
        assert isinstance(metadata, dict)  # noqa: S101
        started = time.monotonic()
        kubectl("delete", "pod", metadata["name"], f"--namespace={NAMESPACE}", "--wait=true")
        elapsed = time.monotonic() - started
        if elapsed >= 30:
            raise RuntimeError("The pod ignored SIGTERM until the termination period ended")
        print(f"The pod shut down in {elapsed:.1f}s")
        kubectl(
            "rollout", "status", f"deployment/{WORKLOAD}", f"--namespace={NAMESPACE}",
            "--timeout=180s",
        )  # fmt: skip
        print("Kubernetes replaced the deleted pod")
    finally:
        kubectl(
            "delete", "--kustomize", f"{root}/services/{DIRECTORY}/deploy", "--ignore-not-found"
        )
        # The overlay also deploys the gateway, which `make gateway-check` covers.
        kubectl(
            "delete", "--kustomize", f"{root}/services/api-gateway/deploy", "--ignore-not-found"
        )
        # The image cannot be removed while a terminating pod still uses it.
        kubectl(
            "wait", "--for=delete", "pod", f"--namespace={NAMESPACE}",
            f"--selector=app.kubernetes.io/name={WORKLOAD}", "--timeout=60s",
            check=False,
        )  # fmt: skip
        run("docker", "image", "rm", IMAGE, env=env, check=False)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="windops-cluster-") as temporary:
        root = Path(temporary) / "repository"
        shutil.copytree(REPOSITORY, root, ignore=IGNORED)
        check_deployment(root)
    print("The manifests work on the cluster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
