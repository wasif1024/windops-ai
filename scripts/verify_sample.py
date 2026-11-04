#!/usr/bin/env python3
"""Prove two real services work together on the local cluster, through the gateway.

Run `make cluster` first. The sample services and gateway stay running afterwards.
"""

import time

import httpx
from gateway_routes import port_forward
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


def main() -> int:
    for target in ("images", "secrets", "infra", "services"):
        run("make", target)
    for workload, namespace in WORKLOADS:
        kubectl("rollout", "status", workload, f"--namespace={namespace}", "--timeout=300s")
    run("make", "gateway-routes")
    with port_forward("service/windops-gateway", SYSTEM, 80) as port:
        response = request_until_ok(f"http://127.0.0.1:{port}/sample/echo")
    body = response.json()
    if body != {"message": "hello", "tenant_id": "sample"}:
        raise RuntimeError(f"Unexpected response: {body}")
    print(f"Gateway, then caller over HTTP, then responder over signed gRPC returned: {body}")
    for workload, namespace in WORKLOADS:
        name = workload.split("/")[1]
        statuses = kubectl(
            "get", "pods", f"--namespace={namespace}",
            f"--selector=app.kubernetes.io/name={name}",
            "--output=jsonpath={.items[*].status.containerStatuses[0].restartCount}",
        ).stdout  # fmt: skip
        if any(count != "0" for count in statuses.split()):
            raise RuntimeError(f"{name} has restarted: {statuses}")
    print("No workload has restarted")
    print(
        kubectl(
            "top", "pod", f"--namespace={APPLICATION}", "--no-headers", check=False
        ).stdout.rstrip()
    )
    print("The sample services work on the cluster.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
