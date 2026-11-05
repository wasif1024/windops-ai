#!/usr/bin/env python3
"""Prove Qdrant works on the real local cluster, and keeps its data.

Run `make secrets` and `make infra` first. This restarts Qdrant once, and removes its test
collection afterwards.
"""

import ssl
import subprocess
import sys
import time
from pathlib import Path

import grpc
import httpx
from gateway_routes import port_forward
from tls_client import authority_file
from windops_events import tls_context

CONTEXT = "windops"
NAMESPACE = "windops-data"
STATEFULSET = "statefulset/windops-qdrant"
POD = "windops-qdrant-0"
COLLECTION = "qdrant_check"
SECRETS_FILE = (
    Path(__file__).resolve().parent.parent
    / "infrastructure"
    / "minikube"
    / "secrets.local.qdrant.env"
)
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
POINTS = [
    {"id": 1, "vector": [1.0, 0.0, 0.0], "payload": {"tenant_id": TENANT_A}},
    {"id": 2, "vector": [0.9, 0.1, 0.0], "payload": {"tenant_id": TENANT_B}},
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={NAMESPACE}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def wait_ready() -> None:
    kubectl("rollout", "status", STATEFULSET, "--timeout=180s")
    kubectl("wait", f"pod/{POD}", "--for=condition=Ready", "--timeout=180s")


def tls() -> ssl.SSLContext:
    return tls_context(authority_file())


def client(port: int, key: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"https://127.0.0.1:{port}",
        headers={"api-key": key},
        timeout=30,
        verify=tls(),
    )


def search_tenant(api: httpx.Client, tenant: str) -> list[int]:
    """Return the points a query finds, using the tenant filter DAT-006 requires."""
    response = api.post(
        f"/collections/{COLLECTION}/points/query",
        json={
            "query": [1.0, 0.0, 0.0],
            "limit": 10,
            "filter": {"must": [{"key": "tenant_id", "match": {"value": tenant}}]},
        },
    )
    response.raise_for_status()
    return [point["id"] for point in response.json()["result"]["points"]]


def check_access(port: int) -> None:
    url = f"https://127.0.0.1:{port}/collections"
    missing = httpx.get(url, timeout=10, verify=tls())
    check(missing.status_code == 401, "a request with no API key is refused")
    wrong = httpx.get(url, headers={"api-key": "wrong-key"}, timeout=10, verify=tls())
    check(wrong.status_code == 401, "a wrong API key is refused")


def check_grpc_access() -> None:
    """The gRPC port must need the key too, or it would be a way around the HTTP check."""
    code = None
    with (
        port_forward(f"pod/{POD}", NAMESPACE, 6334) as port,
        grpc.secure_channel(
            f"127.0.0.1:{port}",
            grpc.ssl_channel_credentials(Path(authority_file()).read_bytes()),
        ) as channel,
    ):
        call: grpc.UnaryUnaryMultiCallable[bytes, bytes] = channel.unary_unary(
            "/qdrant.Collections/List"
        )
        try:
            call(b"", timeout=10)
        except grpc.RpcError as error:
            code = error.code()
    check(code is grpc.StatusCode.UNAUTHENTICATED, "the gRPC port also refuses a missing API key")


def check_no_usage_reporting() -> None:
    """EGR-001: read the setting from the running process, not from the manifest."""
    disabled = kubectl("exec", POD, "--", "printenv", "QDRANT__TELEMETRY_DISABLED").strip()
    check(disabled == "true", "Qdrant runs with usage reporting turned off")


def create_collection(api: httpx.Client) -> None:
    api.delete(f"/collections/{COLLECTION}").raise_for_status()
    api.put(
        f"/collections/{COLLECTION}", json={"vectors": {"size": 3, "distance": "Cosine"}}
    ).raise_for_status()
    api.put(
        f"/collections/{COLLECTION}/index",
        params={"wait": "true"},
        json={"field_name": "tenant_id", "field_schema": "keyword"},
    ).raise_for_status()
    api.put(
        f"/collections/{COLLECTION}/points", params={"wait": "true"}, json={"points": POINTS}
    ).raise_for_status()
    check(search_tenant(api, TENANT_A) == [1], "a tenant filter returns only that tenant's vectors")
    check(search_tenant(api, TENANT_B) == [2], "another tenant sees only its own vectors")


def check_restart(key: str) -> None:
    kubectl("delete", "pod", POD)
    wait_ready()
    with port_forward(f"pod/{POD}", NAMESPACE, 6333) as port, client(port, key) as api:
        check(search_tenant(api, TENANT_A) == [1], "vectors survive a pod restart (SRS 8.1)")


def check_backup_and_restore(api: httpx.Client) -> None:
    """Make the calls that infrastructure/data/qdrant/README.md documents."""
    created = api.post(f"/collections/{COLLECTION}/snapshots", params={"wait": "true"})
    created.raise_for_status()
    name = created.json()["result"]["name"]
    backup = api.get(f"/collections/{COLLECTION}/snapshots/{name}")
    backup.raise_for_status()
    api.delete(f"/collections/{COLLECTION}").raise_for_status()
    check(
        api.get(f"/collections/{COLLECTION}/exists").json()["result"]["exists"] is False,
        "the collection is gone before the restore",
    )
    restored = api.post(
        f"/collections/{COLLECTION}/snapshots/upload",
        params={"priority": "snapshot", "wait": "true"},
        files={"snapshot": (name, backup.content)},
    )
    restored.raise_for_status()
    check(search_tenant(api, TENANT_A) == [1], "a backup restores the vectors (DEP-006)")


def main() -> int:
    if not SECRETS_FILE.exists():
        print("No Qdrant API key found. Run: make secrets", file=sys.stderr)
        return 1
    key = SECRETS_FILE.read_text().strip().partition("=")[2]
    try:
        wait_ready()
        with port_forward(f"pod/{POD}", NAMESPACE, 6333) as port:
            check_access(port)
        check_grpc_access()
        check_no_usage_reporting()
        with port_forward(f"pod/{POD}", NAMESPACE, 6333) as port, client(port, key) as api:
            create_collection(api)
        check_restart(key)
        # The restart ended the earlier port-forward, so each stage opens its own.
        time.sleep(1)
        with port_forward(f"pod/{POD}", NAMESPACE, 6333) as port, client(port, key) as api:
            check_backup_and_restore(api)
    except (RuntimeError, OSError, httpx.HTTPError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        try:
            with port_forward(f"pod/{POD}", NAMESPACE, 6333) as port, client(port, key) as api:
                api.delete(f"/collections/{COLLECTION}")
        except (RuntimeError, OSError, httpx.HTTPError) as error:
            print(f"Could not remove the test collection: {error}", file=sys.stderr)
    print("Qdrant check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
