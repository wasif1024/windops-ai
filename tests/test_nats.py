"""Check the NATS manifests, logins and subject owners against SRS sections 7 and 11."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from windops_events import SUBJECTS

from scripts.setup.local_secrets import nats_secrets
from scripts.setup.nats_access import EVENT_SERVICES, PUBLISHERS, configuration
from scripts.setup.provision_postgres import ADMIN_KEY, SERVICE_NAMESPACES, database_name

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "infrastructure" / "kubernetes" / "base" / "data"
PASSWORDS = {ADMIN_KEY: "admin-value", **{key: f"value-{key}" for key in EVENT_SERVICES}}


def documents() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(STACK)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


@pytest.fixture(scope="module")
def workload() -> dict[str, Any]:
    return next(
        item
        for item in documents()
        if item["kind"] == "StatefulSet" and item["metadata"]["name"] == "windops-nats"
    )


def users() -> dict[str, Any]:
    config = json.loads(configuration(PASSWORDS))
    return {item["user"]: item for item in config["authorization"]["users"]}


def test_nats_is_in_the_data_namespace_and_not_exposed() -> None:
    """DEP-007. The monitoring port must not be on the Service."""
    service = next(
        item
        for item in documents()
        if item["kind"] == "Service" and item["metadata"]["name"] == "windops-nats"
    )
    assert service["metadata"]["namespace"] == "windops-data"
    assert service["spec"]["type"] == "ClusterIP"
    assert [port["port"] for port in service["spec"]["ports"]] == [4222]


def test_streams_are_stored_on_a_persistent_volume(workload: dict[str, Any]) -> None:
    """DEP-006."""
    claim = workload["spec"]["volumeClaimTemplates"][0]["metadata"]["name"]
    mounts = workload["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {"name": claim, "mountPath": "/data"} in mounts
    assert json.loads(configuration(PASSWORDS))["jetstream"]["store_dir"] == "/data"


def test_nats_is_pinned_probed_and_limited(workload: dict[str, Any]) -> None:
    container = workload["spec"]["template"]["spec"]["containers"][0]
    assert "@sha256:" in container["image"]
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(container)
    assert "memory" in container["resources"]["limits"]


def test_nats_meets_the_restricted_pod_security_profile(workload: dict[str, Any]) -> None:
    pod = workload["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    context = pod["containers"][0]["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"]["drop"] == ["ALL"]


def test_metrics_come_from_a_limited_exporter_that_needs_no_login(workload: dict[str, Any]) -> None:
    """SRS 10.3: it reads the local monitoring port, so it holds no NATS password."""
    pod = workload["spec"]["template"]
    assert pod["metadata"]["annotations"]["prometheus.io/port"] == "7777"
    exporter = pod["spec"]["containers"][1]
    assert "@sha256:" in exporter["image"]
    assert "-jsz=all" in exporter["args"]
    assert "env" not in exporter
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(exporter)
    assert "memory" in exporter["resources"]["limits"]
    assert exporter["securityContext"]["readOnlyRootFilesystem"] is True
    assert exporter["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_every_login_needs_a_password() -> None:
    config = json.loads(configuration(PASSWORDS))
    assert "no_auth_user" not in config
    assert all(item["password"] for item in config["authorization"]["users"])


def test_each_subject_has_an_owner() -> None:
    owned = {subject for subjects in PUBLISHERS.values() for subject in subjects}
    assert owned == set(SUBJECTS)


def test_every_publisher_has_a_login() -> None:
    assert set(PUBLISHERS) <= set(EVENT_SERVICES)


def test_publishers_match_the_service_map() -> None:
    text = (ROOT / "docs" / "service-map.md").read_text()
    table = text[
        text.index("## Every service and its links") : text.index("The telemetry simulator")
    ]
    rows = [line.split("|") for line in table.splitlines() if line.startswith("| ")]
    documented: dict[str, set[str]] = {}
    for row in rows[2:]:
        name = row[1].strip().lower().replace(" ", "-")
        subjects = set(re.findall(r"`([a-z]+\.[a-z]+)`", row[3]))
        if subjects:
            documented[name] = subjects
    assert documented == {service: set(subjects) for service, subjects in PUBLISHERS.items()}


@pytest.mark.parametrize("service", sorted(EVENT_SERVICES))
def test_a_service_can_publish_only_the_subjects_it_owns(service: str) -> None:
    allowed = users()[database_name(service)]["permissions"]["publish"]["allow"]
    domain = {subject for subject in allowed if subject in SUBJECTS}
    assert domain == set(PUBLISHERS.get(service, ()))
    assert ">" not in allowed


@pytest.mark.parametrize("service", sorted(SERVICE_NAMESPACES))
def test_a_service_can_ask_for_audit_records_only_in_its_own_name(service: str) -> None:
    allowed = users()[database_name(service)]["permissions"]["publish"]["allow"]
    requests = [subject for subject in allowed if subject.startswith("audit.requested")]
    assert requests == [f"audit.requested.{service}"]


def test_a_service_with_no_database_cannot_ask_for_audit_records() -> None:
    allowed = users()[database_name("web-bff")]["permissions"]["publish"]["allow"]
    assert not [subject for subject in allowed if subject.startswith("audit.requested")]


def test_only_approval_can_publish_a_decision() -> None:
    owners = [
        name
        for name, item in users().items()
        if "approval.decided" in item.get("permissions", {}).get("publish", {}).get("allow", [])
    ]
    assert owners == [database_name("approval")]


@pytest.mark.parametrize("action", ["CREATE", "UPDATE", "DELETE", "PURGE"])
def test_services_cannot_change_streams(action: str) -> None:
    for name, item in users().items():
        if name != "windops_admin":
            assert f"$JS.API.STREAM.{action}.>" in item["permissions"]["publish"]["deny"]


def test_services_cannot_subscribe_to_events_directly() -> None:
    """Reading goes through consumers, so a position and retries always apply."""
    for name, item in users().items():
        if name != "windops_admin":
            assert item["permissions"]["subscribe"]["allow"] == ["_INBOX.>"]


def test_a_password_cannot_change_the_configuration() -> None:
    hostile = {**PASSWORDS, "audit": 'x", "permissions": {}, "y": "'}
    config = json.loads(configuration(hostile))
    audit = next(
        item for item in config["authorization"]["users"] if item["user"] == "windops_audit"
    )
    assert audit["password"] == hostile["audit"]
    assert ">" not in audit["permissions"]["publish"]["allow"]


def test_each_secret_reaches_only_its_service_namespace() -> None:
    by_name: dict[str, Any] = {
        str(item["metadata"]["name"]): item  # type: ignore[index]
        for item in nats_secrets(PASSWORDS)
    }
    assert by_name["windops-nats-config"]["metadata"]["namespace"] == "windops-data"
    for service, namespace in EVENT_SERVICES.items():
        secret = by_name[f"windops-{service}-events"]
        assert secret["metadata"]["namespace"] == namespace
        assert secret["stringData"]["password"] == PASSWORDS[service]
        assert PASSWORDS[ADMIN_KEY] not in json.dumps(secret)
