"""Check the Redis manifests and logins against SRS sections 8 and 11."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.setup.local_secrets import redis_secrets
from scripts.setup.provision_postgres import ADMIN_KEY, SERVICE_NAMESPACES, database_name
from scripts.setup.redis_access import CACHE_SERVICES, acl_file

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "infrastructure" / "kubernetes" / "base" / "data"
PASSWORDS = {ADMIN_KEY: "admin-value", **{key: f"value-{key}" for key in CACHE_SERVICES}}


@pytest.fixture(scope="module")
def workload() -> dict[str, Any]:
    return next(
        item
        for item in _documents()
        if item["metadata"]["name"] == "windops-redis" and item["kind"] == "Deployment"
    )


def _documents() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(STACK)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def test_redis_is_in_the_data_namespace_and_not_exposed() -> None:
    """DEP-007."""
    service = next(
        item
        for item in _documents()
        if item["kind"] == "Service" and item["metadata"]["name"] == "windops-redis"
    )
    assert service["metadata"]["namespace"] == "windops-data"
    assert service["spec"]["type"] == "ClusterIP"


def test_redis_keeps_nothing_across_a_restart(workload: dict[str, Any]) -> None:
    """EVT-005: nothing durable can come to rely on Redis."""
    assert "volumeClaimTemplates" not in workload["spec"]
    # The settings are arguments, after the script that reloads its certificate.
    command = workload["spec"]["template"]["spec"]["containers"][0]["args"]
    assert command[command.index("--save") + 1] == ""
    assert command[command.index("--appendonly") + 1] == "no"


def test_redis_memory_is_bounded(workload: dict[str, Any]) -> None:
    container = workload["spec"]["template"]["spec"]["containers"][0]
    assert "--maxmemory" in container["args"]
    assert "memory" in container["resources"]["limits"]


def test_redis_is_pinned_and_probed(workload: dict[str, Any]) -> None:
    container = workload["spec"]["template"]["spec"]["containers"][0]
    assert "@sha256:" in container["image"]
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(container)


def test_redis_meets_the_restricted_pod_security_profile(workload: dict[str, Any]) -> None:
    pod = workload["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    context = pod["containers"][0]["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"]["drop"] == ["ALL"]


def test_default_login_is_off() -> None:
    assert acl_file(PASSWORDS).splitlines()[0] == "user default off resetchannels -@all"


@pytest.mark.parametrize("password", ["a b", "a\nuser x on", "a~*", "", "a>b"])
def test_password_that_could_change_the_rules_is_refused(password: str) -> None:
    with pytest.raises(ValueError, match="unsupported characters"):
        acl_file({**PASSWORDS, "audit": password})


def test_each_login_reaches_only_its_own_keys() -> None:
    """ARC-002."""
    lines = acl_file(PASSWORDS).splitlines()
    for service in SERVICE_NAMESPACES:
        line = next(item for item in lines if item.startswith(f"user {database_name(service)} "))
        assert f" ~{service}:* " in line
        assert " -@all " in line
        assert "~*" not in line
        assert "+@all" not in line


@pytest.mark.parametrize("command", ["+flushall", "+flushdb", "+config", "+keys", "+eval", "+acl"])
def test_service_logins_cannot_run_dangerous_commands(command: str) -> None:
    lines = acl_file(PASSWORDS).splitlines()[2:]
    assert all(command not in line for line in lines)


def test_each_secret_reaches_only_its_service_namespace() -> None:
    by_name: dict[str, Any] = {
        str(item["metadata"]["name"]): item  # type: ignore[index]
        for item in redis_secrets(PASSWORDS)
    }
    for name in ("windops-redis-acl", "windops-redis-admin"):
        assert by_name[name]["metadata"]["namespace"] == "windops-data"
    for service, namespace in SERVICE_NAMESPACES.items():
        secret = by_name[f"windops-{service}-cache"]
        assert secret["metadata"]["namespace"] == namespace
        assert secret["stringData"]["password"] == PASSWORDS[service]
        assert PASSWORDS[ADMIN_KEY] not in json.dumps(secret)


def test_service_logins_can_run_what_the_library_sends() -> None:
    """The Redis client sends INCRBY for an increment, as a real cluster run showed."""
    for line in acl_file(PASSWORDS).splitlines()[2:]:
        for command in ("+get", "+set", "+del", "+incrby", "+expire", "+multi", "+exec"):
            assert f" {command} " in f"{line} "
