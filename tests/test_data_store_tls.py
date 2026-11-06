"""Every data store accepts only TLS, and every client checks its certificate (GRP-005)."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "infrastructure" / "data"
STORES = ("postgres", "redis", "qdrant", "nats")
TLS = "/var/run/windops/tls"


def rendered(directory: Path) -> list[dict[str, Any]]:
    output = subprocess.run(
        ["kubectl", "kustomize", str(directory)], capture_output=True, text=True, check=True
    ).stdout
    return [item for item in yaml.safe_load_all(output) if item]


def workload(store: str) -> dict[str, Any]:
    return next(i for i in rendered(DATA / store) if i["kind"] in {"StatefulSet", "Deployment"})


def main_container(store: str) -> dict[str, Any]:
    container: dict[str, Any] = workload(store)["spec"]["template"]["spec"]["containers"][0]
    return container


@pytest.mark.parametrize("store", STORES)
def test_a_store_has_a_certificate_for_its_address_and_for_port_forwards(store: str) -> None:
    certificate = next(i for i in rendered(DATA / store) if i["kind"] == "Certificate")["spec"]
    assert f"windops-{store}.windops-data.svc.cluster.local" in certificate["dnsNames"]
    assert "127.0.0.1" in certificate["ipAddresses"]
    assert certificate["usages"] == ["server auth"]
    assert certificate["issuerRef"]["name"] == "windops-internal"


@pytest.mark.parametrize("store", STORES)
def test_a_store_mounts_its_certificate_read_only(store: str) -> None:
    mount = next(m for m in main_container(store)["volumeMounts"] if m["name"] == "tls")
    assert mount["mountPath"] == TLS
    assert mount["readOnly"] is True


def test_postgres_has_no_rule_that_accepts_a_plain_network_login() -> None:
    config = next(i for i in rendered(DATA / "postgres") if i["kind"] == "ConfigMap")
    rules = [line.split()[0] for line in config["data"]["pg_hba.conf"].splitlines() if line]
    assert rules == ["local", "hostssl"]
    arguments = main_container("postgres")["args"]
    assert "ssl=on" in arguments
    assert "hba_file=/etc/postgres/pg_hba.conf" in arguments


def test_redis_has_its_plain_port_off() -> None:
    arguments = main_container("redis")["args"]
    assert arguments[arguments.index("--port") + 1] == "0"
    assert arguments[arguments.index("--tls-port") + 1] == "6379"


def test_qdrant_turns_on_tls_and_its_probes_use_it() -> None:
    container = main_container("qdrant")
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["QDRANT__SERVICE__ENABLE_TLS"] == "true"
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["scheme"] == "HTTPS"


def test_nats_requires_tls_and_services_are_given_a_tls_address() -> None:
    from scripts.setup.nats_access import EVENT_SERVICES, URL, configuration

    config = json.loads(configuration(dict.fromkeys(["admin", *EVENT_SERVICES], "x")))
    assert config["tls"] == {"cert_file": f"{TLS}/tls.crt", "key_file": f"{TLS}/tls.key"}
    assert URL.startswith("tls://")


@pytest.mark.parametrize("store", STORES)
def test_a_store_watches_its_certificate_for_renewal(store: str) -> None:
    script = main_container(store)["command"][2]
    assert f"{TLS}/tls.crt" in script
    assert "exec " in script


def test_keycloak_checks_the_database_certificate() -> None:
    items = rendered(ROOT / "infrastructure" / "security" / "keycloak")
    deployment = next(i for i in items if i["kind"] in {"StatefulSet", "Deployment"})
    pod = deployment["spec"]["template"]["spec"]
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert "sslmode=verify-full" in env["KC_DB_URL_PROPERTIES"]
    assert_mounts_only_the_authority(pod)


def assert_mounts_only_the_authority(pod: dict[str, Any]) -> None:
    volume = next(v for v in pod["volumes"] if v["name"] == "trust")
    assert volume["secret"]["secretName"] == "windops-trust"
    assert volume["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]


@pytest.mark.parametrize("job", sorted(ROOT.glob("services/*/deploy/migrate-job.yaml")))
def test_a_migration_job_mounts_only_the_authority(job: Path) -> None:
    manifest = yaml.safe_load(job.read_text())
    pod = manifest["spec"]["template"]["spec"]
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["WINDOPS_TLS_DIR"] == TLS
    assert_mounts_only_the_authority(pod)


def test_the_trust_certificate_cannot_log_in_to_a_service() -> None:
    items = rendered(ROOT / "infrastructure" / "security" / "pki")
    trust = [i for i in items if i["metadata"]["name"] == "windops-trust"]
    assert {i["metadata"]["namespace"] for i in trust} >= {"windops-system", "windops-application"}
    for item in trust:
        assert "client auth" not in item["spec"]["usages"]
        assert "server auth" not in item["spec"]["usages"]
