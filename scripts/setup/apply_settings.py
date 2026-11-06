#!/usr/bin/env python3
"""Put the WINDOPS_ settings into the cluster, and restart what reads them if any changed.

    make config

Every workload reads the `windops-settings` ConfigMap in its namespace. The values come from
scripts/lib/settings.py: the process environment, then .env, then .env.example.
"""

import json
import subprocess
import sys

from scripts.lib.settings import KUBE_CONTEXT, ROOT, cluster_settings

NAME = "windops-settings"
NAMESPACES = ("windops-system", "windops-application")


def kubectl(*arguments: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def current(namespace: str) -> dict[str, str]:
    found = kubectl(
        "get", "configmap", NAME, f"--namespace={namespace}", "--ignore-not-found", "--output=json"
    )  # fmt: skip
    data: dict[str, str] = json.loads(found).get("data", {}) if found.strip() else {}
    return data


def readers(namespace: str) -> list[str]:
    """The deployments whose pods read the ConfigMap."""
    listed = json.loads(kubectl("get", "deployments", f"--namespace={namespace}", "--output=json"))
    names = []
    for item in listed["items"]:
        containers = item["spec"]["template"]["spec"]["containers"]
        sources = [source for container in containers for source in container.get("envFrom", [])]
        if any(source.get("configMapRef", {}).get("name") == NAME for source in sources):
            names.append(item["metadata"]["name"])
    return names


def apply(namespace: str, data: dict[str, str]) -> bool:
    """Return True if the values changed."""
    before = current(namespace)
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": NAME,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "windops"},
        },
        "data": data,
    }
    kubectl("apply", "--filename=-", stdin=json.dumps(manifest))
    return bool(before) and before != data


def main() -> int:
    try:
        data = cluster_settings()
        # The namespaces may not exist yet on a new cluster.
        kubectl("apply", "--kustomize", str(ROOT / "infrastructure/kubernetes/base/namespaces"))
        for namespace in NAMESPACES:
            changed = apply(namespace, data)
            restart = readers(namespace) if changed else []
            for deployment in restart:
                kubectl(
                    "rollout", "restart", f"deployment/{deployment}", f"--namespace={namespace}"
                )
            note = f", restarted {', '.join(restart)}" if restart else ""
            print(f"{NAME} in {namespace}: {len(data)} settings{note}")
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
