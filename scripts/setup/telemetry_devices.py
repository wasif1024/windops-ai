#!/usr/bin/env python3
"""Give each demo turbine a device credential, and keep the tokens in the simulator's Secret.

Run `make migrate`, `make services` and `make asset-seed` first. Safe to repeat. The credentials
are made inside the Telemetry Service's pod, with its own database login. Each token is shown only
once, so it goes straight into the Secret `windops-simulator-devices`, one key per turbine, and is
never printed. A turbine missing from the Secret gets a new credential, which revokes its old one.
Pass --replace to replace them all.
"""

import argparse
import base64
import json
import subprocess
import sys

from windops_simulator.fleet import DEMO_TURBINES

from scripts.lib.settings import KUBE_CONTEXT

NAMESPACE = "windops-application"
DEPLOYMENT = "deployment/windops-telemetry"
# The Secret's name, not a secret.
SECRET = "windops-simulator-devices"  # noqa: S105


def kubectl(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, f"--namespace={NAMESPACE}", *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip


def key_for(tenant_id: str, turbine_id: str) -> str:
    return f"{tenant_id}.{turbine_id}"


def stored_tokens() -> dict[str, str]:
    found = kubectl("get", "secret", SECRET, "--ignore-not-found", "--output=json")
    if found.returncode != 0:
        raise RuntimeError(f"Could not read {SECRET}: {found.stderr.strip()}")
    if not found.stdout.strip():
        return {}
    data = json.loads(found.stdout).get("data") or {}
    return {key: base64.b64decode(value).decode() for key, value in data.items()}


def secret_manifest(tokens: dict[str, str]) -> str:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": SECRET,
                "namespace": NAMESPACE,
                "labels": {"app.kubernetes.io/part-of": "windops"},
            },
            "type": "Opaque",
            "stringData": tokens,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replace", action="store_true", help="replace every turbine's credential")
    arguments = parser.parse_args()
    tokens = {} if arguments.replace else stored_tokens()
    wanted = [
        f"{spec.tenant_id}:{spec.turbine_id}"
        for spec in DEMO_TURBINES
        if key_for(spec.tenant_id, spec.turbine_id) not in tokens
    ]
    if not wanted:
        print(f"Every demo turbine already has a credential in {SECRET}")
        return 0
    # --replace, as a turbine missing from the Secret may still have a credential nobody can use.
    made = kubectl(
        "exec", DEPLOYMENT, "--", "python", "-m", "windops_telemetry.devices", "--replace", *wanted
    )
    if made.returncode != 0:
        print(f"Could not make device credentials: {made.stderr.strip()}", file=sys.stderr)
        return 1
    for line in made.stdout.splitlines():
        item = json.loads(line)
        tokens[key_for(item["tenant_id"], item["turbine_id"])] = item["token"]
    applied = kubectl("apply", "--filename=-", stdin=secret_manifest(tokens))
    if applied.returncode != 0:
        print(f"Could not store the tokens: {applied.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"Made {len(wanted)} device credentials, and kept their tokens in {SECRET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
