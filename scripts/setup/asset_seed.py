#!/usr/bin/env python3
"""Record the synthetic demo wind farms, turbines and components. Safe to repeat.

Run `make migrate` and `make services` first. It runs inside the Asset Service's pod, with the
service's own database login, so nothing here holds a password.
"""

import subprocess
import sys

from scripts.lib.settings import KUBE_CONTEXT

NAMESPACE = "windops-application"
DEPLOYMENT = "deployment/windops-asset"


def main() -> int:
    result = subprocess.run(
        [
            "kubectl", "--context", KUBE_CONTEXT, "exec", DEPLOYMENT, f"--namespace={NAMESPACE}",
            "--", "python", "-m", "windops_asset.seed",
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        print(f"Could not seed the demo assets: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
