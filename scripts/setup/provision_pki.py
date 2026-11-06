#!/usr/bin/env python3
"""Install cert-manager and the internal CA, ready to issue. Safe to repeat."""

import subprocess
import sys
import time
from pathlib import Path

from scripts.lib.settings import KUBE_CONTEXT

CONTEXT = KUBE_CONTEXT
NAMESPACE = "cert-manager"
SECURITY = Path(__file__).resolve().parents[2] / "infrastructure" / "security"
WORKLOADS = ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector")
# cert-manager's webhook refuses requests for a while after it reports ready.
ATTEMPTS = 30
WAIT_SECONDS = 5


def kubectl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments], capture_output=True, text=True, check=False
    )


def main() -> int:
    installed = kubectl(
        "apply", "--server-side", "--force-conflicts", "--kustomize", str(SECURITY / "cert-manager")
    )
    if installed.returncode != 0:
        print(f"Could not install cert-manager: {installed.stderr}", file=sys.stderr)
        return 1
    for workload in WORKLOADS:
        status = kubectl(
            "rollout", "status", f"deployment/{workload}", f"--namespace={NAMESPACE}",
            "--timeout=300s",
        )  # fmt: skip
        if status.returncode != 0:
            print(f"{workload} did not start: {status.stderr}", file=sys.stderr)
            return 1
    result = None
    for _ in range(ATTEMPTS):
        result = kubectl("apply", "--kustomize", str(SECURITY / "pki"))
        if result.returncode == 0:
            break
        time.sleep(WAIT_SECONDS)
    else:
        print(f"Could not create the CA: {result.stderr if result else ''}", file=sys.stderr)
        return 1
    ready = kubectl(
        "wait", "certificate/windops-internal-ca", f"--namespace={NAMESPACE}",
        "--for=condition=Ready", "--timeout=120s",
    )  # fmt: skip
    if ready.returncode != 0:
        print(f"The CA is not ready: {ready.stderr}", file=sys.stderr)
        return 1
    print("cert-manager and the internal CA are ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
