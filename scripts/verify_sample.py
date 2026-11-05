#!/usr/bin/env python3
"""Prove two real services work together on the local cluster, through the gateway.

Run `make cluster` first. The sample services and gateway stay running afterwards.
"""

import json
import time
from typing import Any

import httpx
from gateway_routes import port_forward
from tls_client import trust_cluster_ca
from verify_cluster import kubectl, run

SYSTEM = "windops-system"
APPLICATION = "windops-application"
WORKLOADS = (
    ("deployment/windops-sample-responder", APPLICATION),
    ("deployment/windops-sample-caller", APPLICATION),
    ("deployment/windops-gateway", SYSTEM),
)


def request_until_ok(url: str) -> httpx.Response:
    response = None
    for _ in range(60):
        try:
            response = httpx.get(url, params={"message": "hello"}, timeout=8)
            if response.status_code == 200:
                return response
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError(f"The request never succeeded: {response}")


def crashed(status: dict[str, Any]) -> bool:
    """A clean exit is how a service picks up a renewed certificate, so only a failure counts."""
    if status["restartCount"] == 0:
        return False
    exit_code = status.get("lastState", {}).get("terminated", {}).get("exitCode")
    return bool(exit_code != 0)


def main() -> int:
    trust_cluster_ca()
    for target in ("images", "secrets", "infra", "services"):
        run("make", target)
    for workload, namespace in WORKLOADS:
        kubectl("rollout", "status", workload, f"--namespace={namespace}", "--timeout=300s")
    run("make", "gateway-routes")
    with port_forward("service/windops-gateway", SYSTEM, 443) as port:
        response = request_until_ok(f"https://127.0.0.1:{port}/sample/echo")
    body = response.json()
    if body != {"message": "hello", "tenant_id": "sample"}:
        raise RuntimeError(f"Unexpected response: {body}")
    print(f"Gateway, then caller over HTTP, then responder over signed gRPC returned: {body}")
    for workload, namespace in WORKLOADS:
        name = workload.split("/")[1]
        pods = json.loads(
            kubectl(
                "get", "pods", f"--namespace={namespace}",
                f"--selector=app.kubernetes.io/name={name}", "--output=json",
            ).stdout
        )  # fmt: skip
        for pod in pods["items"]:
            for status in pod["status"]["containerStatuses"]:
                if crashed(status):
                    raise RuntimeError(f"{name} has crashed: {status}")
    print("No workload has crashed")
    print(
        kubectl(
            "top", "pod", f"--namespace={APPLICATION}", "--no-headers", check=False
        ).stdout.rstrip()
    )
    print("The sample services work on the cluster.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
