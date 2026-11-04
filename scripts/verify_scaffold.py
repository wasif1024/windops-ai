#!/usr/bin/env python3
"""Prove the service template works, in a throwaway copy of the repository.

Generates a service, runs the repository's checks, builds its image, then runs the
container with the same restrictions as the Kubernetes manifests.
"""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
DIRECTORY = "scaffold-probe-service"
IMAGE = "windops-scaffold-probe:check"
CONTAINER = "windops-scaffold-probe-check"
IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "assets"
)
HEALTH_SCRIPT = """
import sys
import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
channel = grpc.insecure_channel(f"127.0.0.1:{sys.argv[1]}")
response = health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest(), timeout=5)
sys.exit(0 if response.status == health_pb2.HealthCheckResponse.SERVING else 1)
"""


def run(*command: str, cwd: Path | None = None) -> str:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{command[0]} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_http(url: str, *, attempts: int = 60) -> int:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                return int(response.status)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    raise RuntimeError(f"No response from {url}")


def check_generated_repository(root: Path) -> None:
    run("make", "service", f"NAME={DIRECTORY}", cwd=root)
    # Not `make check`: the copy has no git history for the contract comparison.
    run("make", "lint", "typecheck", "test", cwd=root)
    rendered = run("kubectl", "kustomize", f"services/{DIRECTORY}/deploy", cwd=root)
    for kind in ("ConfigMap", "Deployment", "Service", "PodDisruptionBudget"):
        if f"kind: {kind}" not in rendered:
            raise RuntimeError(f"The manifests have no {kind}")


def check_container(root: Path) -> None:
    run(
        "docker",
        "build",
        "-q",
        "-f",
        f"services/{DIRECTORY}/Dockerfile",
        "-t",
        IMAGE,
        ".",
        cwd=root,
    )
    user = run("docker", "image", "inspect", IMAGE, "--format", "{{.Config.User}}").strip()
    if user != "10001:10001":
        raise RuntimeError(f"The image runs as {user!r}, not a non-root user")

    grpc_port, http_port = free_port(), free_port()
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)
    # A missing secret must stop the service, rather than let it run unprotected.
    refused = subprocess.run(
        ["docker", "run", "--rm", IMAGE], capture_output=True, text=True, check=False
    )
    if refused.returncode == 0 or "WINDOPS_IDENTITY_SECRET" not in refused.stderr:
        raise RuntimeError("The service started without its signing key")

    run(
        "docker", "run", "-d", "--name", CONTAINER,
        "--read-only", "--tmpfs", "/tmp",  # noqa: S108  # A private in-container mount.
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-e", "WINDOPS_IDENTITY_SECRET=" + "x" * 32,
        "-p", f"127.0.0.1:{grpc_port}:50051", "-p", f"127.0.0.1:{http_port}:8080",
        IMAGE,
    )  # fmt: skip
    try:
        if wait_for_http(f"http://127.0.0.1:{http_port}/health/ready") != 200:
            raise RuntimeError("The container is not ready")
        wait_for_http(f"http://127.0.0.1:{http_port}/metrics")
        run("uv", "run", "python", "-c", HEALTH_SCRIPT, str(grpc_port), cwd=root)
        started = time.monotonic()
        run("docker", "stop", "--time", "15", CONTAINER)
        elapsed = time.monotonic() - started
        exit_code = run("docker", "inspect", CONTAINER, "--format", "{{.State.ExitCode}}").strip()
        if exit_code != "0":
            raise RuntimeError(f"Graceful shutdown exited with {exit_code}")
        if elapsed >= 15:
            raise RuntimeError("The service ignored SIGTERM and had to be killed")
        logs = run("docker", "logs", CONTAINER)
        messages = [json.loads(line)["message"] for line in logs.splitlines()]
        for expected in ("Service started", "Service stopping", "Service stopped"):
            if expected not in messages:
                raise RuntimeError(f"The logs have no {expected!r} line:\n{logs}")
        print(f"Stopped gracefully in {elapsed:.1f}s, with {len(messages)} JSON log lines")
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-image", action="store_true", help="Keep the image for scanning")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="windops-scaffold-") as temporary:
        root = Path(temporary) / "repository"
        shutil.copytree(REPOSITORY, root, ignore=IGNORED)
        check_generated_repository(root)
        check_container(root)
    if not arguments.keep_image:
        subprocess.run(["docker", "image", "rm", IMAGE], capture_output=True, check=False)
    print("The service template works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
