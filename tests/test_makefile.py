"""Check that the Makefile offers every SRS command and is honest about unbuilt ones."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# SRS section 11.4 names these commands.
SRS_TARGETS = {
    "images",
    "secrets",
    "infra",
    "identity-seed",
    "migrate",
    "services",
    "seed-demo",
    "smoke-test",
    "test",
    "eval",
    "chaos",
    "logs",
    "clean",
}
BUILT_TARGETS = {
    "help",
    "install",
    "lint",
    "format",
    "typecheck",
    "test",
    "audit",
    "contracts",
    "check",
    "service",
    "images",
    "scaffold-check",
    "cluster",
    "cluster-stop",
    "cluster-delete",
    "cluster-check",
    "infra",
    "secrets",
    "clean",
    "gateway",
    "gateway-routes",
    "gateway-forward",
    "gateway-check",
    "observability-forward",
    "observability-check",
    "logs",
    "services",
    "contracts-gen",
    "sample-check",
    "phase-1-gate",
    "postgres-provision",
    "postgres-check",
    "redis-check",
}
PLANNED_TARGETS = SRS_TARGETS - BUILT_TARGETS


def run_make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("target", sorted(SRS_TARGETS | BUILT_TARGETS))
def test_target_exists(target: str) -> None:
    # `make -n` prints the recipe without running it, and fails for an unknown target.
    result = run_make("-n", target)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("target", sorted(PLANNED_TARGETS))
def test_planned_target_fails_and_names_its_module(target: str) -> None:
    result = run_make(target)
    assert result.returncode != 0
    assert re.search(r"Module M\d+\.\d+ delivers it", result.stderr)


def test_every_planned_module_exists_in_the_tracker() -> None:
    tracker = (ROOT / "docs" / "phases" / "README.md").read_text()
    for target in sorted(PLANNED_TARGETS):
        message = run_make(target).stderr
        match = re.search(r"Module (M\d+\.\d+) delivers it", message)
        assert match is not None, target
        assert f"**{match.group(1)} " in tracker, target
