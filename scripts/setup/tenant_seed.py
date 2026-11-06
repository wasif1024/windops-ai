#!/usr/bin/env python3
"""Record the demo tenants in the Tenant Service, whose organizations make identity-seed made.

Safe to repeat. Run `make identity-seed` and `make services` first.
"""

import subprocess
import sys

from scripts.lib.settings import KUBE_CONTEXT
from scripts.setup.identity_seed import PLATFORM_TENANT, TENANTS

CONTEXT = KUBE_CONTEXT
NAMESPACE = "windops-system"
DEPLOYMENT = "deployment/windops-tenant"


def main() -> int:
    for tenant, name in {**TENANTS, PLATFORM_TENANT: "WindOps Platform"}.items():
        result = subprocess.run(
            [
                "kubectl", "--context", CONTEXT, "exec", DEPLOYMENT, f"--namespace={NAMESPACE}",
                "--", "python", "-m", "windops_tenant.bootstrap",
                "--tenant", tenant, "--name", name,
            ],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if result.returncode != 0:
            print(f"Could not record {tenant}: {result.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"{tenant}: {result.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
