#!/usr/bin/env python3
"""Run each service's migration Job in turn, once per release, stopping at the first failure.

make migrate
"""

import subprocess
import sys
import time
from pathlib import Path

import yaml
from settings import KUBE_CONTEXT

CONTEXT = KUBE_CONTEXT
REPOSITORY = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 660


class MigrationError(RuntimeError):
    """A migration Job failed, so nothing after it should run."""


def _kubectl(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip


def job_manifests(root: Path = REPOSITORY) -> list[Path]:
    """Return every service's Job in a fixed order, so each release runs them the same way."""
    return sorted(root.glob("services/*/deploy/migrate-job.yaml"))


def job_state(name: str, namespace: str) -> str | None:
    """Return "Complete" or "Failed" once a Job has finished, and None until then."""
    result = _kubectl(
        "get", "job", name, f"--namespace={namespace}",
        "--output=jsonpath={.status.conditions[?(@.status==\"True\")].type}",
    )  # fmt: skip
    if result.returncode != 0:
        raise MigrationError(f"Could not read Job {name}: {result.stderr}")
    conditions = result.stdout.split()
    for state in ("Failed", "Complete"):
        if state in conditions:
            return state
    return None


def run_job(manifest: str, *, timeout_seconds: float = TIMEOUT_SECONDS) -> None:
    """Replace a finished Job with a new run of it, and wait for the result."""
    metadata = yaml.safe_load(manifest)["metadata"]
    name, namespace = metadata["name"], metadata["namespace"]
    # A Job cannot be changed once created, and an old one would hide this release's result.
    _kubectl("delete", "job", name, f"--namespace={namespace}", "--ignore-not-found", "--wait=true")
    applied = _kubectl("apply", "--filename=-", stdin=manifest)
    if applied.returncode != 0:
        raise MigrationError(f"Could not create Job {name}: {applied.stderr}")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = job_state(name, namespace)
        if state == "Complete":
            print(f"{name} finished")
            return
        if state == "Failed":
            logs = _kubectl("logs", f"job/{name}", f"--namespace={namespace}", "--tail=20")
            raise MigrationError(f"{name} failed. Its last log lines:\n{logs.stdout}")
        time.sleep(1)
    raise MigrationError(f"{name} did not finish within {timeout_seconds} seconds")


def main() -> int:
    manifests = job_manifests()
    if not manifests:
        print("No service has migrations yet.")
        return 0
    try:
        for path in manifests:
            run_job(path.read_text())
    except MigrationError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Ran migrations for {len(manifests)} services")
    return 0


if __name__ == "__main__":
    sys.exit(main())
